"""v0.79.0 (§4) — ask lifecycle: settle-edits, inbound gate, supersession,
numbered free-text anchors, canonical Q-numbers, reply reattachment.

Drives the ``/internal/channel/ask`` + ``/internal/channel/send_to_topic``
handlers directly with a minimal ``_FakeRequest``, a REAL ``EngagementRegistry``
(so durable numbering is exercised) and a fake ``claude_code`` driver injected
via ``agent.active_claude_code_driver`` (the same seam production uses).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from broker_helpers import deliver, wait_until
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.options_keyboards: list[dict] = []
        self.sent_texts: list[tuple] = []
        self.edits: list[dict] = []
        self._next_id = 9000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
    ) -> int | None:
        self.options_keyboards.append(
            {"question": question, "options": list(options),
             "request_id": request_id})
        mid = self._next_id
        self._next_id += 1
        return mid

    async def send_to_topic(self, thread_id, text, **kwargs) -> int:
        # A6 (spec §D1): the anchor poster now posts via the single-attempt
        # plain ``send_to_topic``. Records into the same ``sent_texts`` ledger.
        self.sent_texts.append((thread_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def send_response_to_topic(self, topic_id, text) -> int:
        self.sent_texts.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.edits.append(
            {"message_id": message_id, "text": text,
             "clear_keyboard": clear_keyboard})
        return True

    # R2b/c: the ask/anchor post + lifecycle edits route through the rich
    # primitives; the fake records identically (no wire render).
    async def post_ask_body_rich(self, thread_id, text, **kwargs) -> int:
        return await self.send_to_topic(thread_id, text, **kwargs)

    async def edit_topic_message_rich(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        return await self.edit_topic_message(
            topic_id, message_id, text, clear_keyboard=clear_keyboard)


class _FakeDriver:
    """Fake claude_code driver backed by a REAL ``OutputSequencer`` (§2).

    The discrete-intent seam delegates to the real registry so the deferred
    relay-mediated posting model (review C1) is exercised end-to-end. Since
    these tests have no live topic-stream relay, ``arm_send_intent`` SIMULATES
    the relay reaching the intent's tool_use block right after arm — it
    schedules ``post_for_block`` on the real sequencer, which invokes the
    handler-installed poster (posts the keyboard/anchor/reply and records the
    outcome), exactly as the production relay would.
    """

    def __init__(self, engagement_id: str = "e", topic_id: int = 42) -> None:
        from channels.output_sequencer import OutputSequencer

        self.depth = 0
        self.gen = 0
        self.refusals = 0
        self._relay_tasks: list = []

        async def _noop_send(topic, text, reply_to=None):
            return None

        async def _noop_edit(topic, mid, text):
            return True

        self.seq = OutputSequencer(
            engagement_id=engagement_id, topic_id=topic_id,
            send_message=_noop_send, edit_message=_noop_edit)

    # inbound gate reads
    def inbound_unread_depth(self, eid) -> int:
        return self.depth

    def inbound_generation(self, eid) -> int:
        return self.gen

    def record_ask_refusal(self, eid) -> int:
        self.refusals += 1
        return self.refusals

    # discrete-intent seam (delegates to the real sequencer registry)
    def register_send_intent(self, *, engagement_id, request_id, tool_name,
                             projection_hash, poster, on_retire=None):
        return self.seq.register_intent(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster, on_retire=on_retire)

    def set_send_intent_poster(self, eid, rid, poster):
        return self.seq.set_intent_poster(rid, poster)

    def arm_send_intent(self, eid, rid):
        intent = self.seq.arm_intent(rid)
        if intent is not None:
            # Simulate the relay reaching this block just after arm.
            self._relay_tasks.append(asyncio.ensure_future(
                self.seq.post_for_block(intent.tool_name, intent.projection_hash)))
        return intent

    def cancel_send_intent(self, eid, rid):
        return self.seq.cancel_intent(rid)

    def send_intent_outcome(self, eid, rid):
        return self.seq.intent_outcome(rid)

    async def mark_send_intent_posted(self, eid, rid, mid):
        return await self.seq.mark_intent_posted(rid, mid)

    async def await_send_intent(self, eid, rid, timeout=None):
        # F3: let the simulated relay tasks (scheduled on arm) run, then return
        # the recorded outcome so the handler returns ok only when the post
        # landed.
        if self._relay_tasks:
            await asyncio.gather(*self._relay_tasks, return_exceptions=True)
            self._relay_tasks.clear()
        return await self.seq.await_intent_resolution(rid, timeout)

    def intent_state(self, rid):
        intent = self.seq.registry.by_request_id(rid)
        return intent.state if intent is not None else None


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
async def env(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _FakeChannel()
    driver = _FakeDriver()
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)

    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    ask = handlers["/internal/channel/ask"]
    send = handlers["/internal/channel/send_to_topic"]
    return {
        "reg": reg, "rec": rec, "ch": ch, "driver": driver,
        "broker": fresh_broker, "ask": ask, "send": send,
    }


def _ask_payload(**over) -> dict:
    base = {
        "engagement_id": "PLACEHOLDER", "request_id": "rid-1",
        "question": "Proceed?", "options": ["A", "B"], "timeout_s": 60,
        "projection_hash": "hash-abc",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Settle-edit copies (clear_keyboard) + canonical Q-number
# ---------------------------------------------------------------------------


async def _until(cond, *, timeout: float = 5.0):
    """Wait until *cond* holds, bounded by WALL CLOCK rather than by a count
    of scheduler turns.

    The previous version spun 2000 bare ``sleep(0)`` yields. Bare yields never
    advance wall time, so any step that needs a real timer, a thread or an
    executor cannot complete inside them however many turns pass — and on a
    loaded CI runner that is exactly what happens, giving "condition never
    became true" for a condition that was merely late rather than false. It
    passed on a fast, idle machine and failed on a slow, busy one, which is
    the property a deadline removes and a turn count cannot.

    A short real sleep both yields and lets wall time pass, so the deadline
    measures what the caller actually means: how long they are willing to
    wait."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if cond():
            return
        if loop.time() >= deadline:
            raise AssertionError(
                f"condition never became true within {timeout}s")
        await asyncio.sleep(0.001)


async def test_answered_settles_with_check_and_clears_keyboard(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    task = asyncio.ensure_future(env["ask"](
        _FakeRequest(_ask_payload(engagement_id=eid, engagement_token=tok, request_id="a1"))))
    await _until(lambda: (env["broker"].get_meta(
        namespace="engagement_ask", scope=eid, request_id="a1") or {}).get(
        "message_id") is not None)
    assert deliver(env["broker"], 
        namespace="engagement_ask", scope=eid, request_id="a1",
        option_index=1, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    # drain_hooks contractually returns only after the finish hooks (and thus
    # the settle edit) have run — asserting directly after it keeps that
    # contract pinned instead of papering over a premature return.
    await env["broker"].drain_hooks()

    assert _body(resp) == {
        "ok": True, "outcome": "answered", "option": "B", "option_index": 1}
    # Settle edit: PRESENT clear_keyboard, BOUNDED positional ✅ copy appended
    # BELOW the canonical body (v0.84.0 D1 bullet 3 — never the full label).
    edit = env["ch"].edits[-1]
    assert edit["clear_keyboard"] is True
    assert edit["text"] == "Q1: Proceed?\n\n1. A\n2. B\n✅ Option 2"


async def test_expired_settles_with_hourglass_and_clears_keyboard(env, monkeypatch):
    import channels.channel_handlers  # noqa: F401
    eid = env["rec"].id
    tok = env["rec"].auth_token
    # Fire the timeout immediately.
    task = asyncio.ensure_future(env["ask"](
        _FakeRequest(_ask_payload(engagement_id=eid, engagement_token=tok, request_id="e1", timeout_s=30))))
    # Fire the synthetic timeout only once the keyboard post has landed (CI
    # flake on a loaded runner: a fixed 0.02 s sleep lost to the handler's
    # real awaits, the _on_timeout no-opped on a not-yet-live key, and the
    # task only settled at the real 30 s deadline — TimeoutError). The POST
    # is the boundary, not broker registration: registration strictly
    # precedes it, and the expiry settle edits the posted message — firing
    # between the two would settle with no message to edit.
    await wait_until(lambda: env["ch"].options_keyboards)
    env["broker"]._on_timeout(("engagement_ask", eid, "e1"))
    resp = await asyncio.wait_for(task, timeout=10.0)
    await env["broker"].drain_hooks()

    assert _body(resp) == {"ok": True, "outcome": "no_answer"}
    edit = env["ch"].edits[-1]
    assert edit["clear_keyboard"] is True
    assert edit["text"] == (
        "Q1: Proceed?\n\n1. A\n2. B\n"
        "⌛ expired — engagement paused; reply here to continue")


async def test_canonical_qnumber_prepends_verbatim(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    # v0.85.0 (round 4, D4): the agent's own "Q7:" prefix is preserved
    # VERBATIM — Casa only PREPENDS the allocated durable number, it no
    # longer strips an agent-authored leading "Q<digits>:".
    task = asyncio.ensure_future(env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="c1", question="Q7: Which DB?"))))
    # Await the observable (keyboard actually posted), not a fixed sleep.
    await wait_until(lambda: env["ch"].options_keyboards)
    posted_q = env["ch"].options_keyboards[-1]["question"]
    assert posted_q == "Q1: Q7: Which DB?\n\n1. A\n2. B"
    # open_questions ledger + summary accessor agree with the message. The
    # ledger write is a separate awaited step after the post — wait for it
    # too before delivering, or the settle-close races the open-write.
    await wait_until(lambda: env["reg"].open_question_numbers(eid) == [1])
    deliver(env["broker"], 
        namespace="engagement_ask", scope=eid, request_id="c1",
        option_index=0, actor_id=555)
    await asyncio.wait_for(task, timeout=1.0)
    await env["broker"].drain_hooks()
    # Settled → closed in the ledger.
    assert env["reg"].open_question_numbers(eid) == []


# ---------------------------------------------------------------------------
# Free-text anchor (options: [])
# ---------------------------------------------------------------------------


async def test_free_text_anchor_posts_numbered_and_registers(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="ft1",
        question="What's the DB name?", options=[])))
    body = _body(resp)
    assert body["ok"] is True and body["outcome"] == "anchored"
    assert body["question_number"] == 1
    # Posting is RELAY-DEFERRED (§2, C1): the numbered anchor posts when the
    # relay reaches the ask tool_use block. Drive the (simulated) relay.
    await asyncio.sleep(0.01)
    # Posted as a plain numbered anchor (NO keyboard) + registered open.
    assert env["ch"].sent_texts[-1] == (42, "Q1: What's the DB name?")
    assert env["ch"].options_keyboards == []
    assert env["reg"].open_question_numbers(eid) == [1]


# ---------------------------------------------------------------------------
# Inbound gate + escalation
# ---------------------------------------------------------------------------


async def test_unread_inbound_refuses_without_registering(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    env["driver"].depth = 1  # operator message waiting, unseen
    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="g1")))
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "unread_inbound"
    assert "end your turn now" in body["message"]
    assert body["refusal_count"] == 1
    # No keyboard posted, no live broker request, intent tombstoned.
    assert env["ch"].options_keyboards == []
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []
    assert env["driver"].intent_state("g1") == "cancelled"


async def test_free_text_anchor_also_gated_on_unread(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    env["driver"].depth = 1
    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="ga1", question="DB name?", options=[])))
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "unread_inbound"
    # No anchor posted, nothing registered.
    assert env["ch"].sent_texts == []
    assert env["reg"].open_question_numbers(eid) == []


async def test_refusal_escalates_at_third(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    env["driver"].depth = 1
    msgs = []
    for i in range(3):
        resp = await env["ask"](_FakeRequest(_ask_payload(
            engagement_id=eid, engagement_token=tok, request_id=f"r{i}")))
        msgs.append(_body(resp)["message"])
    assert msgs[0] == msgs[1]
    assert msgs[2] != msgs[0]
    assert "STOP ASKING" in msgs[2]


# ---------------------------------------------------------------------------
# Generation re-check supersession
# ---------------------------------------------------------------------------


async def test_generation_recheck_supersedes(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    ch = env["ch"]
    driver = env["driver"]

    # An operator message lands in the register→post window: bump the
    # generation the moment the keyboard is posted.
    orig_post = ch.post_options_keyboard

    async def _post(*, engagement_id, request_id, question, options):
        driver.gen += 1  # operator envelope arrived during the post
        return await orig_post(
            engagement_id=engagement_id, request_id=request_id,
            question=question, options=options)

    ch.post_options_keyboard = _post

    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="s1")))
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "superseded"
    await env["broker"].drain_hooks()
    # Keyboard settled with the superseded copy + cleared.
    edit = ch.edits[-1]
    assert edit["clear_keyboard"] is True
    assert edit["text"] == (
        "Q1: Proceed?\n\n1. A\n2. B\n🚫 superseded by your message below")


# ---------------------------------------------------------------------------
# Reply reattachment (response-loss-after-post)
# ---------------------------------------------------------------------------


async def test_reply_retry_reattaches_no_double_post(env):
    eid = env["rec"].id
    tok = env["rec"].auth_token
    p = {"engagement_id": eid, "engagement_token": tok, "text": "hello operator",
         "request_id": "rep-1", "projection_hash": "rh"}
    r1 = await env["send"](_FakeRequest(p))
    b1 = _body(r1)
    assert b1["ok"] is True  # armed; posting is relay-deferred (§2, C1)
    # Drive the (simulated) relay: the poster posts ONCE and records the outcome.
    await asyncio.sleep(0.01)
    assert env["ch"].sent_texts.count((42, "hello operator")) == 1
    first_mid = env["driver"].send_intent_outcome(eid, "rep-1")["message_id"]
    assert first_mid is not None

    # A transport retry with the SAME request_id must reattach — one post only.
    r2 = await env["send"](_FakeRequest(p))
    b2 = _body(r2)
    assert b2 == {"ok": True, "message_id": first_mid}
    assert env["ch"].sent_texts.count((42, "hello operator")) == 1


# ---------------------------------------------------------------------------
# F3 — fail-closed deferred posting (ok:true + outcome ok:false impossible)
# ---------------------------------------------------------------------------


class _FailingChannel(_FakeChannel):
    """A channel whose topic send FAILS (returns None) — models a transient
    Telegram failure of the relay-deferred reply/anchor post."""

    async def send_to_topic(self, thread_id, text, **kwargs) -> int | None:
        # A6 (spec §D1): the anchor poster's wire — fail it too so the anchor
        # fail-closed test still models a failed anchor post.
        self.sent_texts.append((thread_id, text))
        return None

    async def send_response_to_topic(self, topic_id, text) -> int | None:
        self.sent_texts.append((topic_id, text))
        return None


@pytest.fixture
async def failing_env(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _FailingChannel()
    driver = _FakeDriver()
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "ch": ch, "driver": driver,
        "broker": fresh_broker, "ask": handlers["/internal/channel/ask"],
        "send": handlers["/internal/channel/send_to_topic"],
    }


async def test_reply_poster_failure_returns_ok_false(failing_env):
    """F3: the deferred reply poster fails → the handler AWAITS the outcome and
    returns ok:false. An ok:true response with a failed post is impossible."""
    eid = failing_env["rec"].id
    tok = failing_env["rec"].auth_token
    r = await failing_env["send"](_FakeRequest({
        "engagement_id": eid, "engagement_token": tok, "text": "shipped it",
        "request_id": "rf-1", "projection_hash": "rh"}))
    assert _body(r)["ok"] is False
    # The intent recorded an ok:false outcome (surfaced, not swallowed).
    outcome = failing_env["driver"].send_intent_outcome(eid, "rf-1")
    assert outcome is not None and outcome["ok"] is False


async def test_anchor_poster_failure_returns_ok_false(failing_env):
    """F3: the free-text anchor poster fails → ok:false, never ok:true."""
    eid = failing_env["rec"].id
    tok = failing_env["rec"].auth_token
    r = await failing_env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="af-1",
        question="Which DB?", options=[])))
    assert _body(r)["ok"] is False


# ---------------------------------------------------------------------------
# F5 — deferred retry fail-closed + anchor no-double-number-allocation.
# ---------------------------------------------------------------------------


class _UnresolvedDriver:
    """A driver whose deferred intent is ARMED but the relay never posts — so a
    retry reattaches to an UNRESOLVED intent and ``await_send_intent`` times out
    to None. Models the F5 fail-open probe: the handler must NOT return ok:true
    on an unresolved intent."""

    def __init__(self, created: bool = False) -> None:
        self.depth = 0
        self.gen = 0
        self.refusals = 0
        self._created = created

    def inbound_unread_depth(self, eid) -> int:
        return self.depth

    def inbound_generation(self, eid) -> int:
        return self.gen

    def record_ask_refusal(self, eid) -> int:
        self.refusals += 1
        return self.refusals

    def register_send_intent(self, *, engagement_id, request_id, tool_name,
                             projection_hash, poster, on_retire=None):
        return (object(), self._created)

    def set_send_intent_poster(self, eid, rid, poster):
        return None

    def arm_send_intent(self, eid, rid):
        return None

    def cancel_send_intent(self, eid, rid):
        return None

    def send_intent_outcome(self, eid, rid):
        return None  # unresolved: no recorded outcome

    async def await_send_intent(self, eid, rid, timeout=None):
        return None  # bounded resolution timed out


async def test_reply_retry_unresolved_awaits_and_fails_closed(env, monkeypatch):
    """F5: a reply RETRY reattaching to an unresolved intent AWAITS the same
    bounded resolution; a None/timeout maps to ok:false — never ok:true with no
    post."""
    driver = _UnresolvedDriver(created=False)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    eid = env["rec"].id
    tok = env["rec"].auth_token
    r = await env["send"](_FakeRequest({
        "engagement_id": eid, "engagement_token": tok, "text": "hi",
        "request_id": "rep-u",
        "projection_hash": "rh"}))
    b = _body(r)
    assert b["ok"] is False and b["error"] == "send_failed"
    assert env["ch"].sent_texts == []  # nothing posted


async def test_reply_first_attempt_unresolved_fails_closed(env, monkeypatch):
    """F5: even the FIRST attempt fails closed when the post never resolves
    (outcome None) — the old code returned ok:true with message_id None."""
    driver = _UnresolvedDriver(created=True)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    eid = env["rec"].id
    tok = env["rec"].auth_token
    r = await env["send"](_FakeRequest({
        "engagement_id": eid, "engagement_token": tok, "text": "hi",
        "request_id": "rep-f",
        "projection_hash": "rh"}))
    assert _body(r)["ok"] is False


async def test_anchor_retry_unresolved_fails_closed_no_number(env, monkeypatch):
    """F5: a free-text anchor RETRY reattaching to an unresolved intent fails
    closed AND does not burn a fresh Q-number (reattach check precedes number
    allocation)."""
    driver = _UnresolvedDriver(created=False)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    eid = env["rec"].id
    tok = env["rec"].auth_token
    before = env["reg"].get(eid).next_question_number
    r = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, engagement_token=tok, request_id="fu", question="DB?", options=[])))
    b = _body(r)
    assert b["ok"] is False and b["error"] == "delivery_failed"
    assert env["reg"].get(eid).next_question_number == before  # no number burned
    assert env["ch"].sent_texts == []


async def test_anchor_retry_reattaches_without_new_qnumber(env):
    """F5: a free-text anchor transport RETRY reattaches — NO new Q-number, NO
    second anchor, single open-question entry (parity with the button reattach).
    """
    eid = env["rec"].id
    tok = env["rec"].auth_token
    p = _ask_payload(engagement_id=eid, engagement_token=tok, request_id="ftr",
                     question="DB?", options=[])
    r1 = await env["ask"](_FakeRequest(p))
    assert _body(r1)["question_number"] == 1
    await asyncio.sleep(0.01)
    assert env["reg"].get(eid).next_question_number == 2  # 1 allocated → next 2
    sent_before = list(env["ch"].sent_texts)

    r2 = await env["ask"](_FakeRequest(p))
    b2 = _body(r2)
    assert b2["ok"] is True and b2["outcome"] == "anchored"
    # No fresh number allocated on the reattach (old code allocated at line 577
    # BEFORE the reattach check → next would advance to 3):
    assert env["reg"].get(eid).next_question_number == 2
    assert env["ch"].sent_texts == sent_before          # no second anchor
    assert env["reg"].open_question_numbers(eid) == [1]  # single open question


# ---------------------------------------------------------------------------
# F1 (Sol r3) — button-ask reattach/creation race must not lose static metadata
# ---------------------------------------------------------------------------


async def test_button_ask_reattach_race_preserves_static_metadata(env, monkeypatch):
    """F1: a button-ask transport RETRY that CREATES the broker request while
    the first attempt is suspended in number allocation must not strip the
    keyboard's static metadata.

    Regression (Sol r3 re-probe): the reattach path created the broker request
    WITHOUT options/topic_id/operator_id; the first attempt then resumed, saw
    ``created=False`` and skipped the meta init, leaving broker meta =
    {"message_id": ...} only ⇒ every tap rejected.
    """
    eid = env["rec"].id
    tok = env["rec"].auth_token
    reg = env["reg"]

    parked = asyncio.Event()   # set once the first attempt is inside allocation
    resume = asyncio.Event()   # released after the retry has registered
    orig_alloc = reg.allocate_question_number
    calls = {"n": 0}

    async def _slow_alloc(engagement_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt suspends INSIDE allocation; hand off to the retry.
            parked.set()
            await resume.wait()
        return await orig_alloc(engagement_id)

    monkeypatch.setattr(reg, "allocate_question_number", _slow_alloc)

    # Observe the retry actually reaching the owner-first gate: wrap the
    # gate's event.wait so the test proceeds only once the retry is parked on
    # it (a blind yield loop cannot prove that — it is still a race).
    from channels.channel_handlers import get_or_create_gate
    gate = get_or_create_gate("race-1")
    reached_gate = asyncio.Event()
    orig_wait = gate.event.wait

    async def _observed_wait():
        reached_gate.set()
        return await orig_wait()

    monkeypatch.setattr(gate.event, "wait", _observed_wait)

    p = _ask_payload(engagement_id=eid, engagement_token=tok, request_id="race-1")
    first = asyncio.ensure_future(env["ask"](_FakeRequest(p)))
    await asyncio.wait_for(parked.wait(), timeout=1.0)

    # The RETRY (same request_id) reattaches while the first attempt is still
    # parked; the owner-first gate blocks it until the owner registers the
    # broker request and marks the gate PASSED, so the OWNER creates the
    # request — the reattach-created branch is pinned by the no-local-owner
    # test below.
    second = asyncio.ensure_future(env["ask"](_FakeRequest(p)))
    await asyncio.wait_for(reached_gate.wait(), timeout=1.0)

    # Let the first attempt resume; registration and meta seeding follow on
    # the owner path. Wait on the registered request instead of a fixed sleep
    # — the fixed 0.02s raced the scheduler under CI load.
    resume.set()
    await _until(lambda: env["broker"].get_meta(
        namespace="engagement_ask", scope=eid, request_id="race-1") is not None)

    meta = env["broker"].get_meta(
        namespace="engagement_ask", scope=eid, request_id="race-1")
    assert meta is not None
    assert meta.get("topic_id") == 42
    assert meta.get("operator_id") == 555
    assert meta.get("options") == ["A", "B"]

    # A tap is accepted — it resolves the ask to "answered" (the meta the
    # inline-callback handler validates against — topic_id/operator_id/options —
    # is all present, so the tap is not rejected).
    assert deliver(env["broker"], 
        namespace="engagement_ask", scope=eid, request_id="race-1",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(first, timeout=1.0)
    assert _body(resp)["outcome"] == "answered"
    await asyncio.gather(second, return_exceptions=True)
    await env["broker"].drain_hooks()


async def test_reattach_without_local_owner_creates_request_with_full_metadata(env):
    """F1, reattach-path create branch: a reattacher that finds a PENDING
    gate with no local validation owner falls through to the broker reattach,
    creates the request itself, and must seed the complete static metadata.

    The setup is deliberately synthetic — the owner safety net in the
    handler's ``finally`` resolves the gate before dropping ownership, so
    normal in-process flow does not produce this state. It is the code's own
    documented fail-safe contract, though: the reattach register seeds meta
    "if THIS reattach wins the create race", and the gate-cleanup path names
    "a reattacher that found a PENDING gate with no local owner" as a handled
    case. The owner-first gate means the parked-owner race above can no
    longer reach this branch — the owner always wins creation there — so
    this test pins the branch directly. Red case demonstrated: dropping
    ``meta=_ask_static_meta()`` from the reattach-path ``BROKER.register`` in
    channel_handlers.py fails this test (and only this test)."""
    from channels.output_sequencer import ASK_TOOL

    eid = env["rec"].id
    tok = env["rec"].auth_token

    async def _vanished_owner_poster():
        return None

    # The intent exists — registered by an owner that vanished before creating
    # the broker request or resolving its gate. The handler's own registration
    # then sees created=False, a PENDING gate, and no local owner, and falls
    # through to the broker reattach that CREATES the request.
    env["driver"].register_send_intent(
        engagement_id=eid, request_id="orphan-1", tool_name=ASK_TOOL,
        projection_hash="hash-abc", poster=_vanished_owner_poster)

    task = asyncio.ensure_future(env["ask"](_FakeRequest(
        _ask_payload(engagement_id=eid, engagement_token=tok, request_id="orphan-1"))))
    await _until(lambda: env["broker"].get_meta(
        namespace="engagement_ask", scope=eid, request_id="orphan-1") is not None)

    meta = env["broker"].get_meta(
        namespace="engagement_ask", scope=eid, request_id="orphan-1")
    assert meta.get("topic_id") == 42
    assert meta.get("operator_id") == 555
    assert meta.get("options") == ["A", "B"]

    assert deliver(env["broker"], 
        namespace="engagement_ask", scope=eid, request_id="orphan-1",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    assert _body(resp)["outcome"] == "answered"
    await env["broker"].drain_hooks()


# ---------------------------------------------------------------------------
# W-R1 (Sol r2-2): confirmed-edit settle gating on the finish hook
# ---------------------------------------------------------------------------


class _FailEditChannel:
    """Channel whose settle edit is transiently failing (returns False, as
    ``edit_topic_message`` does on a timeout / non-'not-modified' BadRequest)."""

    def __init__(self, succeed_on: int | None = None) -> None:
        self.attempts = 0
        self.succeed_on = succeed_on  # 1-based attempt that starts succeeding
        self.edits: list[dict] = []

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.attempts += 1
        ok = self.succeed_on is not None and self.attempts >= self.succeed_on
        self.edits.append(
            {"message_id": message_id, "text": text,
             "clear_keyboard": clear_keyboard, "ok": ok})
        return ok

    async def edit_topic_message_rich(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        return await self.edit_topic_message(
            topic_id, message_id, text, clear_keyboard=clear_keyboard)


async def test_finish_hook_preserves_ledger_on_unconfirmed_edit():
    """R1: an all-failing settle edit → the finish hook retries EXACTLY 3× with
    0.5s→1s→2s backoff (injected clock) and DOES NOT close the ledger entry."""
    from channels.channel_handlers import _ask_keyboard_finish

    ch = _FailEditChannel(succeed_on=None)  # never succeeds
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    closed = {"n": 0}

    async def _on_settle():
        closed["n"] += 1

    hook = _ask_keyboard_finish(
        ch, 42, 101, "Q1: Proceed?\n\n1. A\n2. B", ["A", "B"],
        on_settle=_on_settle, sleep=_sleep)
    await hook({"outcome": "answered", "option_index": 0})

    assert ch.attempts == 3           # exactly 3 bounded attempts
    assert sleeps == [0.5, 1.0, 2.0]  # 0.5→1→2 backoff via injected clock
    assert closed["n"] == 0           # ledger entry PRESERVED (no premature close)


async def test_finish_hook_closes_once_when_edit_confirmed_on_retry_two():
    """R1: edit succeeds on the SECOND attempt → ledger closed exactly once."""
    from channels.channel_handlers import _ask_keyboard_finish

    ch = _FailEditChannel(succeed_on=2)  # fail 1, confirm on 2
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    closed = {"n": 0}

    async def _on_settle():
        closed["n"] += 1

    hook = _ask_keyboard_finish(
        ch, 42, 101, "Q1: Proceed?\n\n1. A\n2. B", ["A", "B"],
        on_settle=_on_settle, sleep=_sleep)
    await hook({"outcome": "answered", "option_index": 1})

    assert ch.attempts == 2
    assert sleeps == [0.5]   # slept once after the first failure, then confirmed
    assert closed["n"] == 1  # ledger closed exactly once


# ---------------------------------------------------------------------------
# A8 · Q1-settle observability — one INFO line per CONFIRMED button settle
# ---------------------------------------------------------------------------


async def test_finish_hook_logs_confirmed_settle(caplog):
    """A8: a confirmed button-ask settle emits one INFO
    ``ask settle CONFIRMED (eng=… q=… mid=… outcome=…)`` line, with the outcome
    derived from the broker outcome dict (answered / no_answer→expired /
    cancelled+reason→superseded/withdrawn/cancelled)."""
    from channels.channel_handlers import _ask_keyboard_finish

    async def _sleep(_d):
        pass

    cases = [
        ({"outcome": "answered", "option_index": 0}, "answered"),
        ({"outcome": "no_answer"}, "expired"),
        ({"outcome": "cancelled", "reason": "superseded_by_text"}, "superseded"),
        ({"outcome": "cancelled", "reason": "internal_error"}, "withdrawn"),
        ({"outcome": "cancelled"}, "cancelled"),
    ]
    for outcome, expected in cases:
        ch = _FailEditChannel(succeed_on=1)  # confirm on first attempt
        hook = _ask_keyboard_finish(
            ch, 42, 101, "Q3: Proceed?\n\n1. A\n2. B", ["A", "B"],
            sleep=_sleep, eng_id="abcdef1234567890", number=3)
        with caplog.at_level("INFO"):
            caplog.clear()
            await hook(outcome)
        line = next(m for m in caplog.messages if "ask settle CONFIRMED" in m)
        assert "eng=abcdef12" in line
        assert "q=3" in line
        assert "mid=101" in line
        assert f"outcome={expected}" in line


async def test_finish_hook_unconfirmed_settle_logs_nothing(caplog):
    from channels.channel_handlers import _ask_keyboard_finish

    async def _sleep(_d):
        pass

    ch = _FailEditChannel(succeed_on=None)  # never confirms
    hook = _ask_keyboard_finish(
        ch, 42, 101, "Q1: Proceed?\n\n1. A\n2. B", ["A", "B"],
        sleep=_sleep, eng_id="abcdef1234567890", number=1)
    with caplog.at_level("INFO"):
        await hook({"outcome": "answered", "option_index": 0})
    assert not any("ask settle CONFIRMED" in m for m in caplog.messages)
