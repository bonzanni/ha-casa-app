"""#648 — ordinary conversation is not a claim on the operator's attention lane.

The defect this pins: `TelegramChannel._handle_serialized` retired the WHOLE
`(resident_ask, dm:<chat>)` scope on any plain-text DM, and since v0.206.0
(#573) a SCHEDULED question lives in exactly that key — so ordinary words
expired a machine-timed question whose answer could never route back to the
session that asked it. `casa/CHANGELOG.md`'s v0.206.0 ending enumeration does
not contain "the operator typed something".

The invariant, stated once: **a pending scheduled question is retired only by an
explicit claim on the lane** — an answering tap, `/new`, an authorization
challenge, a revocation, its own expiry — **or by a human `ask_user` question
that has actually been DELIVERED and is still live**.

Every case drives a REAL entry point (`TelegramChannel._handle` for inbound
text, `tools.ask_user` for a question), never an extracted predicate helper: a
helper-level test would pass with the fix applied at the wrong call site, which
is the defect class in play. Counts are asserted, never statuses.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import scheduled_asks
import verdict_broker
from verdict_broker import VerdictBroker

from test_ask_user import _FakeBot, _drain_bus, _fake_update
from test_scheduled_ask_user import (
    LABEL,
    OPERATOR,
    _FakeChannel,
    _mk_cm,
    _payload,
    _scheduled_origin,
)

pytestmark = pytest.mark.asyncio

OTHER = 4343


# ---------------------------------------------------------------------------
# fixtures / helpers (local copies — fixtures do not cross test modules)
# ---------------------------------------------------------------------------


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


def _channel(bus, bot):
    from channels.telegram import TelegramChannel

    ch = TelegramChannel(bot_token="T", chat_id="0", default_agent="assistant", bus=bus)
    ch._start_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=bot)
    return ch


def _mk_bus():
    from bus import MessageBus

    async def _noop(_msg):
        return None

    bus = MessageBus()
    bus.register("assistant", _noop)
    return bus


def _register(broker, finishes, *, rid, scope, chat_id, scheduled):
    """One raw request in the broker with a COUNTING finish hook."""
    meta = {"options": ["Yes", "No"], "chat_id": chat_id,
            "operator_id": chat_id, "_scope": scope}
    if scheduled:
        meta["scheduled"] = True
    req, created = broker.register(
        namespace="resident_ask", scope=scope, request_id=rid,
        timeout_s=300.0, detached=True, meta=meta,
    )
    assert created is True
    finishes[rid] = []

    async def _hook(outcome, _rid=rid):
        finishes[_rid].append(outcome)

    broker.set_finish_hook(req, _hook)
    return req


def _dm_origin(chat_id=OPERATOR):
    return {
        "role": "assistant", "execution_role": "assistant",
        "channel": "telegram", "chat_id": str(chat_id), "user_id": chat_id,
        "message_type": "channel_in", "source": "telegram",
    }


async def _ask_on(channel, origin, question="Send the invoice?"):
    """Drive the REAL `tools.ask_user` handler against *channel*."""
    import agent as agent_mod
    import tools as tools_mod
    from unittest.mock import MagicMock

    tools_mod.init_tools(
        channel_manager=_mk_cm(channel), bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    tok = agent_mod.origin_var.set(origin)
    try:
        res = await tools_mod.ask_user.handler(
            {"question": question, "options": ["Confirm", "Wrong", "Later"]})
    finally:
        agent_mod.origin_var.reset(tok)
    return _payload(res)


# ---------------------------------------------------------------------------
# 1. the selector truth table, in ONE real drive
# ---------------------------------------------------------------------------


async def test_plain_text_cancels_only_same_dm_interactive(_fresh_broker):
    """RED pre-fix: the scope-wide cancel retires the scheduled request too."""
    bus, bot = _mk_bus(), _FakeBot()
    ch = _channel(bus, bot)
    finishes: dict[str, list] = {}

    _register(_fresh_broker, finishes, rid="interactive-same",
              scope=f"dm:{OPERATOR}", chat_id=OPERATOR, scheduled=False)
    _register(_fresh_broker, finishes, rid="scheduled-same",
              scope=f"dm:{OPERATOR}", chat_id=OPERATOR, scheduled=True)
    _register(_fresh_broker, finishes, rid="authz-same",
              scope=f"authz:{OPERATOR}", chat_id=OPERATOR, scheduled=False)
    _register(_fresh_broker, finishes, rid="interactive-other",
              scope=f"dm:{OTHER}", chat_id=OTHER, scheduled=False)

    await ch._handle(_fake_update(str(OPERATOR), "ordinary words", OPERATOR), None)
    await _fresh_broker.drain_hooks()

    same = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(same) == 1
    assert sum(rid == "scheduled-same" for rid in same) == 1

    authz = _fresh_broker.pending(namespace="resident_ask", scope=f"authz:{OPERATOR}")
    assert len(authz) == 1
    assert sum(rid == "authz-same" for rid in authz) == 1

    other = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OTHER}")
    assert len(other) == 1
    assert sum(rid == "interactive-other" for rid in other) == 1

    assert len(finishes["interactive-same"]) == 1
    assert sum(o == {"outcome": "cancelled", "reason": "typed_answer"}
               for o in finishes["interactive-same"]) == 1
    assert len(finishes["scheduled-same"]) == 0
    assert len(finishes["authz-same"]) == 0
    assert len(finishes["interactive-other"]) == 0

    assert len(await _drain_bus(bus)) == 1


# ---------------------------------------------------------------------------
# 2. the pinned direction: /new MUST still retire, exactly once
# ---------------------------------------------------------------------------


async def test_new_retires_scheduled_once_with_new_session(
    _fresh_broker, _fresh_store,
):
    """GREEN pre-fix. The second /new proves the hook fires exactly once."""
    channel = _FakeChannel()
    payload = await _ask_on(channel, _scheduled_origin())
    assert payload["status"] == "awaiting_user"

    bus, bot = _mk_bus(), _FakeBot()
    ch = _channel(bus, bot)
    await ch._handle(_fake_update(str(OPERATOR), "/new", OPERATOR), None)
    await ch._handle(_fake_update(str(OPERATOR), "/new", OPERATOR), None)
    await _fresh_broker.drain_hooks()

    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
    assert len(channel.scheduled_dispatches) == 1
    assert sum("new_session" in d["text"]
               for d in channel.scheduled_dispatches) == 1
    assert len(channel.edits) == 1
    assert len(_fresh_store.all()) == 0
    assert len(await _drain_bus(bus)) == 0
    assert len(bot.sent) == 2


# ---------------------------------------------------------------------------
# 3. #347: a dropped message retires nothing
# ---------------------------------------------------------------------------


async def test_rate_limited_text_retires_nothing(_fresh_broker, _fresh_store):
    """GREEN pre-fix — the limiter returns before the cancel (#347)."""
    from rate_limit import RateLimiter

    channel = _FakeChannel()
    payload = await _ask_on(channel, _scheduled_origin())
    rid = payload["request_id"]

    bus, bot = _mk_bus(), _FakeBot()
    ch = _channel(bus, bot)
    limiter = RateLimiter(capacity=1, window_s=60.0)
    assert limiter.check(str(OPERATOR)).allowed is True
    ch._rate_limiter = limiter

    await ch._handle(_fake_update(str(OPERATOR), "dropped words", OPERATOR), None)
    await _fresh_broker.drain_hooks()

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == rid for r in live) == 1
    assert len(channel.scheduled_dispatches) == 0
    assert len(channel.edits) == 0
    assert len(_fresh_store.all()) == 1
    assert len(await _drain_bus(bus)) == 0


# ---------------------------------------------------------------------------
# 4-5, 8. the human question takes the lane only once it is DELIVERED and live
# ---------------------------------------------------------------------------


class _BlockingSecondPost(_FakeChannel):
    """`_FakeChannel` whose SECOND keyboard post blocks until released.

    The window between `register` and a confirmed post is where the whole
    displacement question lives, so the test has to be able to stand inside it.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.post_started = asyncio.Event()
        self.release_post = asyncio.Event()
        self.second_request_id: str | None = None

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False):
        if self.posts:
            self.second_request_id = request_id
            self.post_started.set()
            await self.release_post.wait()
        return await super().post_dm_keyboard(
            chat_id=chat_id, request_id=request_id, text=text,
            options=options, short_labels=short_labels)


async def test_delivered_human_ask_displaces_scheduled_after_post_once(
    _fresh_broker, _fresh_store,
):
    """RED pre-fix: the in-flight count is 1, not 2 — `supersede=True` retired
    the scheduled question before the replacement was ever posted."""
    channel = _BlockingSecondPost()
    scheduled_rid = (await _ask_on(channel, _scheduled_origin()))["request_id"]

    task = asyncio.create_task(_ask_on(channel, _dm_origin(), "Celsius?"))
    await asyncio.wait_for(channel.post_started.wait(), 5.0)

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 2
    assert sum(r == scheduled_rid for r in live) == 1
    assert len(channel.scheduled_dispatches) == 0

    channel.release_post.set()
    human = await asyncio.wait_for(task, 5.0)
    await _fresh_broker.drain_hooks()

    assert human["status"] == "awaiting_user"
    assert len(channel.posts) == 2
    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == channel.second_request_id for r in live) == 1
    assert len(channel.scheduled_dispatches) == 1
    assert sum("superseded" in d["text"] for d in channel.scheduled_dispatches) == 1
    assert len(channel.edits) == 1
    assert len(_fresh_store.all()) == 0


async def test_undelivered_human_ask_leaves_scheduled_live(
    _fresh_broker, _fresh_store,
):
    """RED pre-fix: the scheduled question is retired at registration, so a
    replacement that never reaches the screen destroys it for nothing."""
    channel = _FakeChannel()
    scheduled_rid = (await _ask_on(channel, _scheduled_origin()))["request_id"]

    channel._post_result = None
    human = await _ask_on(channel, _dm_origin(), "Celsius?")
    await _fresh_broker.drain_hooks()

    assert len(channel.posts) == 2
    assert sum(p.get("kind") == "delivery_failed" for p in [human]) == 1

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == scheduled_rid for r in live) == 1
    assert len(channel.scheduled_dispatches) == 0
    assert len(channel.edits) == 0
    assert len(_fresh_store.all()) == 1


async def test_plain_text_during_human_post_cancels_replacement_not_scheduled(
    _fresh_broker, _fresh_store,
):
    """RED pre-fix, and the guard the fix itself needs: a replacement cancelled
    while its post was in flight has a `message_id` but is NOT live, so it must
    not displace anything."""
    channel = _BlockingSecondPost()
    scheduled_rid = (await _ask_on(channel, _scheduled_origin()))["request_id"]

    task = asyncio.create_task(_ask_on(channel, _dm_origin(), "Celsius?"))
    await asyncio.wait_for(channel.post_started.wait(), 5.0)

    bus, bot = _mk_bus(), _FakeBot()
    ch = _channel(bus, bot)
    await ch._handle(
        _fake_update(str(OPERATOR), "I'll answer in words", OPERATOR), None)

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == scheduled_rid for r in live) == 1
    assert len(await _drain_bus(bus)) == 1

    channel.release_post.set()
    await asyncio.wait_for(task, 5.0)
    await _fresh_broker.drain_hooks()

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == scheduled_rid for r in live) == 1
    assert sum(r == channel.second_request_id for r in live) == 0
    assert len(channel.scheduled_dispatches) == 0
    assert len(channel.edits) == 1
    assert len(_fresh_store.all()) == 1


# ---------------------------------------------------------------------------
# 6. a RESTORED question gets whatever protection a live one gets
# ---------------------------------------------------------------------------


async def test_restored_scheduled_ask_survives_plain_text(
    _fresh_broker, _fresh_store,
):
    """RED pre-fix. Keying the exclusion on `meta['scheduled']` is what makes
    this pass: `reconcile_at_boot` rebuilds the binding through the same
    `broker_meta`, so a restored ask carries the identical marker."""
    await _fresh_store.put({
        "rid": "restored", "state": scheduled_asks.STATE_LIVE,
        "role": "assistant", "session_scope": LABEL,
        "scope": f"dm:{OPERATOR}", "chat_id": OPERATOR,
        "operator_id": OPERATOR, "message_id": 77,
        "options": ["Confirm", "Wrong"], "body": "Send the invoice?",
        "epoch": 0, "created_at": 0.0, "expires_at": 1_000.0,
    })
    channel = _FakeChannel()
    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
    assert counts["restored"] == 1
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 1

    bus, bot = _mk_bus(), _FakeBot()
    ch = _channel(bus, bot)
    await ch._handle(_fake_update(str(OPERATOR), "ordinary words", OPERATOR), None)
    await _fresh_broker.drain_hooks()

    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == "restored" for r in live) == 1
    assert verdict_broker.BROKER.get_meta(
        namespace="resident_ask", scope=f"dm:{OPERATOR}",
        request_id="restored")["scheduled"] is True
    assert len(channel.scheduled_dispatches) == 0
    assert len(channel.edits) == 0
    assert len(_fresh_store.all()) == 1
    assert len(await _drain_bus(bus)) == 1


# ---------------------------------------------------------------------------
# 7. characterisation: the EXISTING cancel_for_chat selection, pinned first
# ---------------------------------------------------------------------------


async def test_cancel_for_chat_selects_only_same_dm_scheduled(_fresh_broker):
    """GREEN pre-fix and after. `_is_scheduled` is about to be read by two new
    siblings; this pins what its one shipped caller (`authz_grants.py`) gets,
    so donating the predicate cannot silently change a live caller."""
    finishes: dict[str, list] = {}
    _register(_fresh_broker, finishes, rid="scheduled-same",
              scope=f"dm:{OPERATOR}", chat_id=OPERATOR, scheduled=True)
    _register(_fresh_broker, finishes, rid="interactive-same",
              scope=f"dm:{OPERATOR}", chat_id=OPERATOR, scheduled=False)
    _register(_fresh_broker, finishes, rid="scheduled-authz",
              scope=f"authz:{OPERATOR}", chat_id=OPERATOR, scheduled=True)
    _register(_fresh_broker, finishes, rid="scheduled-other",
              scope=f"dm:{OTHER}", chat_id=OTHER, scheduled=True)

    assert scheduled_asks.cancel_for_chat(OPERATOR, "operator_challenge") == 1
    await _fresh_broker.drain_hooks()

    same = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(same) == 1
    assert sum(r == "interactive-same" for r in same) == 1
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 1
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OTHER}")) == 1

    assert len(finishes["scheduled-same"]) == 1
    assert sum(o == {"outcome": "cancelled", "reason": "operator_challenge"}
               for o in finishes["scheduled-same"]) == 1
    assert len(finishes["interactive-same"]) == 0
    assert len(finishes["scheduled-authz"]) == 0
    assert len(finishes["scheduled-other"]) == 0
