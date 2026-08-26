"""#680 — an authorization challenge takes the operator's attention lane only
once its own keyboard is on screen.

The defect this pins: `ChallengeCoordinator.register_challenge` retired the live
SCHEDULED question at ADMISSION, in the same no-await block as its own
`broker.register` and strictly before the owned setup driver posted anything. A
post that then failed left the operator with NEITHER question — the machine-timed
one already edited to expired with its continuation dispatched (INV-JOB-007), and
the human one never delivered.

The rule, stated once: **a challenge displaces a scheduled question only when the
challenge is BOTH delivered and still live**, liveness read from the broker's live
map by EXACT request id, synchronously, with no await before the displacement.
That is the same rule `tools.ask_user` already shipped for the human half (#648).

Separate module on purpose: the accepted red case in
`test_scheduled_ask_attention_lane.py` is FROZEN, and these are its siblings.

Every case drives the REAL `ChallengeCoordinator`, never
`scheduled_asks.cancel_for_chat` / `displace_scheduled_for_chat` — a helper-level
test cannot see an ordering, which is the defect class in play. Counts are
asserted, never statuses.
"""
from __future__ import annotations

import asyncio

import pytest

import authz_grants
import scheduled_asks
import verdict_broker
from verdict_broker import VerdictBroker

from test_scheduled_ask_attention_lane import OTHER, _ask_on, _dm_origin, _register
from test_scheduled_ask_user import (
    LABEL,
    OPERATOR,
    _FakeChannel,
    _scheduled_origin,
)

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


class _GatedChannel(_FakeChannel):
    """`post_dm_keyboard` blocks on a per-request-id gate, so a test can run an
    arbitrary interleaving WHILE a challenge's post is in flight."""

    def __init__(self):
        super().__init__()
        self.gates: dict[str, asyncio.Event] = {}
        self.results: dict[str, object] = {}

    def gate_for(self, rid_prefix: str, result):
        ev = asyncio.Event()
        self.gates[rid_prefix] = ev
        self.results[rid_prefix] = result
        return ev

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        self.calls.append("post")
        for prefix, ev in self.gates.items():
            if prefix in text:
                await ev.wait()
                return self.results[prefix]
        return self._post_result


def _challenge(coordinator, channel, *, key, text="Approve send_email?"):
    return coordinator.register_challenge(
        key, chat_id=OPERATOR, operator_id=OPERATOR, channel=channel,
        challenge_text=text,
    )


# ---------------------------------------------------------------------------
# 1. the delivered path — one displacement, and only the right request
# ---------------------------------------------------------------------------


async def test_delivered_challenge_displaces_exactly_the_scheduled_ask(
    _fresh_broker, _fresh_store,
):
    """A challenge that DOES reach the screen still retires the scheduled
    question exactly once, and touches nothing else on the lane."""
    channel = _FakeChannel()
    scheduled = await _ask_on(channel, _scheduled_origin())
    scheduled_rid = scheduled["request_id"]

    finishes: dict[str, list] = {}
    _register(_fresh_broker, finishes, rid="interactive-same",
              scope=f"dm:{OPERATOR}", chat_id=OPERATOR, scheduled=False)
    _register(_fresh_broker, finishes, rid="scheduled-other",
              scope=f"dm:{OTHER}", chat_id=OTHER, scheduled=True)

    coordinator = authz_grants.ChallengeCoordinator()
    handle = _challenge(coordinator, channel, key=("tool", "args"))
    assert await handle.settled_post() == "posted"
    await _fresh_broker.drain_hooks()

    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 1
    same = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(same) == 1
    assert sum(r == "interactive-same" for r in same) == 1
    assert sum(r == scheduled_rid for r in same) == 0
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OTHER}")) == 1

    assert len(channel.scheduled_dispatches) == 1
    assert sum("operator_challenge" in d["text"]
               for d in channel.scheduled_dispatches) == 1
    assert len(finishes["interactive-same"]) == 0
    assert len(finishes["scheduled-other"]) == 0
    assert len(_fresh_store.all()) == 0


# ---------------------------------------------------------------------------
# 2. cancelled while posting — a message id is not a live replacement
# ---------------------------------------------------------------------------


async def test_challenge_cancelled_while_posting_displaces_nothing(
    _fresh_broker, _fresh_store,
):
    """Kills the mutation that reads `"message_id" in req.meta` instead of the
    exact-rid liveness check. The post SUCCEEDS (an integer id is recorded), but
    the challenge was settled while it was in flight, so no replacement is on
    the lane and the scheduled question must survive."""
    channel = _GatedChannel()
    scheduled = await _ask_on(channel, _scheduled_origin())
    scheduled_rid = scheduled["request_id"]

    gate = channel.gate_for("BLOCKED-A", 4242)
    coordinator = authz_grants.ChallengeCoordinator()
    handle = _challenge(coordinator, channel, key=("a",), text="BLOCKED-A?")
    await asyncio.sleep(0)

    _fresh_broker.cancel(namespace="resident_ask", scope=f"authz:{OPERATOR}",
                         request_id=handle._challenge.rid, reason="superseded")
    gate.set()
    assert await handle.settled_post() == "inactive"
    await _fresh_broker.drain_hooks()

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == scheduled_rid for r in live) == 1
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 0
    assert len(channel.scheduled_dispatches) == 0
    assert len(_fresh_store.all()) == 1


# ---------------------------------------------------------------------------
# 3. a SIBLING challenge does not answer for this one
# ---------------------------------------------------------------------------


async def test_a_sibling_challenge_does_not_license_the_displacement(
    _fresh_broker, _fresh_store,
):
    """Kills the mutation that tests the SCOPE for occupancy
    (`if broker.pending(...)`) rather than THIS rid's membership.

    Two different-key challenges share `authz:{chat}`. A is cancelled while its
    post is blocked, then its post lands an integer id; B is still undelivered
    and therefore still registered. Under the mutation the scope is non-empty
    (B answers for A), so A's driver displaces the scheduled question although
    no delivered replacement exists anywhere on the lane.
    """
    channel = _GatedChannel()
    scheduled = await _ask_on(channel, _scheduled_origin())
    scheduled_rid = scheduled["request_id"]

    gate_a = channel.gate_for("BLOCKED-A", 4242)
    gate_b = channel.gate_for("BLOCKED-B", 4343)

    coordinator = authz_grants.ChallengeCoordinator()
    handle_a = _challenge(coordinator, channel, key=("a",), text="BLOCKED-A?")
    handle_b = _challenge(coordinator, channel, key=("b",), text="BLOCKED-B?")
    # Both drivers must actually be INSIDE their post before we proceed —
    # `sleep(0)` is not a guarantee, and a test that raced here would pass for
    # the wrong reason.
    while len(channel.posts) < 3:
        await asyncio.sleep(0)
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 2

    # Retire ONLY A, then let its post land an integer id. B stays live and
    # undelivered, which is exactly what the mutant would read.
    _fresh_broker.cancel(namespace="resident_ask", scope=f"authz:{OPERATOR}",
                         request_id=handle_a._challenge.rid, reason="superseded")
    gate_a.set()
    assert await handle_a.settled_post() == "inactive"

    # B now fails to deliver, so it never displaces anything either.
    channel.results["BLOCKED-B"] = None
    gate_b.set()
    assert await handle_b.settled_post() == "delivery_failed"
    await _fresh_broker.drain_hooks()

    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 0
    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == scheduled_rid for r in live) == 1
    assert len(channel.scheduled_dispatches) == 0
    assert len(channel.edits) == 0
    assert len(_fresh_store.all()) == 1


# ---------------------------------------------------------------------------
# 4. a second arrival for the same key displaces nothing further
# ---------------------------------------------------------------------------


async def test_a_duplicate_arrival_posts_once_and_displaces_once(
    _fresh_broker, _fresh_store,
):
    """Dedup-by-key returns before the driver is spawned, so a second arrival
    while the first post is still in flight cannot post, drive or displace
    again."""
    channel = _GatedChannel()
    await _ask_on(channel, _scheduled_origin())

    gate = channel.gate_for("BLOCKED-A", 99)
    coordinator = authz_grants.ChallengeCoordinator()
    first = _challenge(coordinator, channel, key=("a",), text="BLOCKED-A?")
    await asyncio.sleep(0)
    second = _challenge(coordinator, channel, key=("a",), text="BLOCKED-A?")
    assert first.created is True
    assert second.created is False

    gate.set()
    assert await first.settled_post() == "posted"
    assert await second.settled_post() == "posted"
    await _fresh_broker.drain_hooks()

    challenge_posts = [p for p in channel.posts if "BLOCKED-A?" in p[2]]
    assert len(challenge_posts) == 1
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
    assert len(channel.scheduled_dispatches) == 1
    assert sum("operator_challenge" in d["text"]
               for d in channel.scheduled_dispatches) == 1


# ---------------------------------------------------------------------------
# 5. a displacement failure never un-delivers the challenge
# ---------------------------------------------------------------------------


async def test_a_failing_displacement_leaves_the_delivered_challenge_live(
    _fresh_broker, _fresh_store, monkeypatch,
):
    """The containment is not decoration: without it the driver's exception
    would propagate and `settled_post()` would misreport a keyboard that is
    demonstrably on screen. Over-showing (both questions) is the safe side."""
    channel = _FakeChannel()
    scheduled = await _ask_on(channel, _scheduled_origin())
    scheduled_rid = scheduled["request_id"]

    def _boom(chat_id, reason):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(scheduled_asks, "displace_scheduled_for_chat", _boom)

    coordinator = authz_grants.ChallengeCoordinator()
    handle = _challenge(coordinator, channel, key=("tool", "args"))
    assert await handle.settled_post() == "posted"
    await _fresh_broker.drain_hooks()

    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 1
    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == scheduled_rid for r in live) == 1
    assert len(channel.scheduled_dispatches) == 0


# ---------------------------------------------------------------------------
# 6. no scheduling point between the liveness check and the displacement
# ---------------------------------------------------------------------------


async def test_the_liveness_check_and_the_displacement_share_one_loop_turn(
    _fresh_broker, _fresh_store, monkeypatch,
):
    """Kills the mutation that inserts an `await` after the liveness check.

    The guard is only worth its words if nothing can run between asking whether
    the challenge is live and acting on the answer — otherwise a tap, a timeout
    or a `/new` arriving in that gap retires the challenge and the displacement
    proceeds against an answer that is no longer true. Every other pin here is
    blind to it, because the two orderings reach the same terminal state and
    differ only in what could have interleaved.

    So the ORDER is what is asserted, using the loop's own scheduler as the
    probe: `pending()` arms a `call_soon` callback, which by definition runs at
    the next scheduling point. With no await between, the displacement wins;
    with one, the callback does.
    """
    order: list[str] = []
    channel = _FakeChannel()
    await _ask_on(channel, _scheduled_origin())

    real_pending = _fresh_broker.pending

    def _probing_pending(*, namespace, scope):
        result = real_pending(namespace=namespace, scope=scope)
        if scope == f"authz:{OPERATOR}":
            asyncio.get_running_loop().call_soon(
                lambda: order.append("next-scheduling-point"))
        return result

    real_displace = scheduled_asks.displace_scheduled_for_chat

    def _probing_displace(chat_id, reason):
        order.append("displace")
        return real_displace(chat_id, reason)

    monkeypatch.setattr(_fresh_broker, "pending", _probing_pending)
    monkeypatch.setattr(scheduled_asks, "displace_scheduled_for_chat",
                        _probing_displace)

    coordinator = authz_grants.ChallengeCoordinator()
    handle = _challenge(coordinator, channel, key=("tool", "args"))
    assert await handle.settled_post() == "posted"
    await _fresh_broker.drain_hooks()

    assert len(order) == 2
    assert order[0] == "displace"
    assert order[1] == "next-scheduling-point"
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
