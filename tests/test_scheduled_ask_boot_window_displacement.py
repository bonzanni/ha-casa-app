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

from broker_helpers import wait_until
from test_scheduled_ask_attention_lane import _ask_on, _ask_on_raw, _dm_origin
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


async def test_reconcile_skips_an_undelivered_challenge_when_testing_lane_idle(
    _fresh_broker, _fresh_store,
):
    """#762 · INV-JOB-008 — the boot half of "delivered and still live".

    `reconcile_at_boot` runs strictly BETWEEN a challenge's registration and its
    post settling. The lane it must judge is the OPERATOR'S ATTENTION, and a
    keyboard that has not reached Telegram is not holding it: refusing here is
    IRREVERSIBLE (`_settle` edits the keyboard to expired and dispatches the
    terminal continuation), while restoring is not — if the post lands, the
    challenge's own driver retires the restored question at DELIVERY with the
    truthful `operator_challenge`, and if it fails the operator keeps the
    question that was already on screen.

    This file's previous CHARACTERISATION of this window asserted the loss
    (`operator_busy == 1`, one edit, one continuation, record dropped) and
    passed. It is replaced rather than kept: the residual it pinned is closed,
    and a test that names a residual which no longer exists is worse than none.
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
    await wait_until(lambda: len(channel.posts) == 1)

    # The premise, established by counting the broker's OWN metadata rather
    # than by trusting the sequencing above: exactly one live authz request,
    # and its keyboard is not on screen.
    authz = _fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")
    assert len(authz) == 1
    assert sum(
        _fresh_broker.get_meta(
            namespace="resident_ask", scope=f"authz:{OPERATOR}",
            request_id=rid,
        ).get("message_id") is None
        for rid in authz
    ) == 1

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

    channel.gate.set()
    await handle.settled_post()
    await _fresh_broker.drain_hooks()

    assert counts.get("restored", 0) == 1
    assert counts.get("operator_busy", 0) == 0
    assert counts.get("revoked_before_reconcile", 0) == 0

    scheduled = _fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(scheduled) == 1
    assert sum(rid == "from-previous-process" for rid in scheduled) == 1
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 0
    assert len(channel.posts) == 1
    assert len(channel.edits) == 0
    assert len(channel.scheduled_dispatches) == 0
    assert len(_fresh_store.all()) == 1
    assert len(scheduled_asks._BOOT_REVOCATIONS) == 0


async def test_reconcile_skips_an_undelivered_human_ask_when_testing_lane_idle(
    _fresh_broker, _fresh_store,
):
    """The half the issue does not name, and the same rule.

    A human `ask_user` registers into `dm:{chat}` WITHOUT `require_idle` and
    sits in the live map while its own post is in flight. The reconciler saw it
    identically to a delivered one; a fix that covered only the authz half
    would have fixed half the lane.
    """
    await _seed_previous_process_record(_fresh_store)
    channel = _BlockingPostChannel()
    channel.gate = asyncio.Event()
    channel.release_to = None            # the human post will fail too

    asking = asyncio.ensure_future(_ask_on_raw(channel, _dm_origin(), "Celsius?"))
    await wait_until(lambda: len(channel.posts) == 1)

    live = _fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(rid != "from-previous-process" for rid in live) == 1
    assert sum(
        _fresh_broker.get_meta(
            namespace="resident_ask", scope=f"dm:{OPERATOR}", request_id=rid,
        ).get("message_id") is None
        for rid in live
    ) == 1

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

    channel.gate.set()
    await asking
    await _fresh_broker.drain_hooks()

    assert counts.get("restored", 0) == 1
    assert counts.get("operator_busy", 0) == 0
    restored = _fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert sum(rid == "from-previous-process" for rid in restored) == 1
    assert len(channel.edits) == 0
    assert len(channel.scheduled_dispatches) == 0
    assert len(_fresh_store.all()) == 1


async def test_a_challenge_delivered_after_a_restore_retires_it_truthfully(
    _fresh_broker, _fresh_store,
):
    """The price of restoring, paid in full and named.

    When the post the reconciler declined to wait for DOES land, the challenge's
    own driver retires the restored question at delivery — one extra keyboard
    edit and a continuation carrying `operator_challenge`, which is the reason
    that actually happened. Compare the base, which told that session
    `operator_busy` for a keyboard that had not arrived.
    """
    import authz_grants

    await _seed_previous_process_record(_fresh_store)
    channel = _BlockingPostChannel()
    channel.gate = asyncio.Event()
    channel.release_to = 99              # this post SUCCEEDS, once released

    coordinator = authz_grants.ChallengeCoordinator()
    handle = coordinator.register_challenge(
        ("tool", "args"), chat_id=OPERATOR, operator_id=OPERATOR,
        channel=channel, challenge_text="Approve send_email?",
    )
    await wait_until(lambda: len(channel.posts) == 1)

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
    assert counts.get("restored", 0) == 1

    channel.gate.set()
    assert await handle.settled_post() == "posted"
    await _fresh_broker.drain_hooks()

    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 1
    assert len(channel.edits) == 1
    assert len(channel.scheduled_dispatches) == 1
    assert sum("operator_challenge" in d["text"]
               for d in channel.scheduled_dispatches) == 1
    assert sum("operator_busy" in d["text"]
               for d in channel.scheduled_dispatches) == 0
    assert len(_fresh_store.all()) == 0
