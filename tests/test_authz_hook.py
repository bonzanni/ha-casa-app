"""Task 7 (A:§3.2): the fail-closed PreToolUse authorization hook + its wiring
into residents and specialists (executors get nothing).

The hook is the enforcement core of v0.76.0. It:
  - passes unprotected tools through untouched (never touches store/coordinator);
  - denies FIRST for engagement provenance (no challenge, no role-assert), then
    for unsupported transport/execution, then for an explicit role mismatch;
  - checks provenance BEFORE consuming any grant (a copied webhook chat/user id
    can never spend a grant);
  - consumes a single-use, argument-bound grant if one exists (allow ⇒ ``{}``);
  - otherwise posts (or reuses) ONE confirmation challenge and denies, naming
    the button — settled_post delivery_failed / inactive get their own denials;
  - wraps the entire protected path fail-closed: every non-CancelledError
    exception at ANY stage is logged and returns a VALID SDK deny;
    CancelledError propagates.

Coordinator/broker/channel test doubles mirror ``tests/test_authz_grants.py``
(the real ``VerdictBroker`` + a recording ``_FakeChannel``) so the settled_post
outcomes are driven END-TO-END through the real coordinator.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agent as agent_mod
import tools as tools_mod
import provenance as provenance_mod
import authz_grants
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantKey, GrantStore,
    canonical_args_hash, make_resident_authz_hook,
    _DENY_ENGAGEMENT_UNAVAILABLE, _DENY_UNSUPPORTED_ORIGIN, _DENY_ROLE_MISMATCH,
    _DENY_UNRENDERABLE, _DENY_PENDING, _DENY_DELIVERY_FAILED, _DENY_INACTIVE,
    _DENY_POSTED, _DENY_INTERNAL, _DENY_NOT_OPERATOR,
)

pytestmark = pytest.mark.unit


TOOL = "mcp__plugin_p_p__invoice_reset"
ARTIFACT = "artifact-1"
# v0.78.0 W1: protected_map values are {"artifact_id", "summary"} — the hook
# consumes ONLY artifact_id (zero enforcement change); summary is unused here.
PROTECTED = {TOOL: {"artifact_id": ARTIFACT, "summary": None}}


# ---------------------------------------------------------------------------
# Origin + hook drivers
# ---------------------------------------------------------------------------


def _origin(**overrides) -> dict:
    """A dm/direct finance origin by default (role == execution_role)."""
    base = {
        "role": "finance",
        "channel": "telegram",
        "chat_id": "42",
        "user_id": 100,
        "cid": "abc",
        "message_type": "channel_in",
        "source": "telegram",
        "execution_role": "finance",
    }
    base.update(overrides)
    return base


def _engagement(**overrides):
    """A realistic in_casa SPECIALIST engagement record (#400): an active
    specialist with a topic and an operator DM in its own origin. The authz hook
    only ``getattr``s these fields, so a SimpleNamespace is a faithful double."""
    base = dict(
        id="eng-abc123", kind="specialist", status="active", topic_id=7,
        role_or_type="finance",
        origin={"user_id": 100, "chat_id": "42"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _OriginCtx:
    """Set agent.origin_var (+ optionally tools.engagement_var) for the block.

    ``engaged=True`` binds a BARE Mock (a non-specialist engagement record — the
    #400 fail-closed case). Pass ``engagement=<record>`` to bind a specific
    record (e.g. ``_engagement()`` for the authorizable specialist path)."""

    def __init__(self, origin: dict | None, *, engaged: bool = False,
                 engagement=None):
        self._origin = origin
        self._engaged = engaged
        self._engagement = engagement

    def __enter__(self):
        self._otok = agent_mod.origin_var.set(self._origin)
        if self._engagement is not None:
            eng = self._engagement
        elif self._engaged:
            eng = Mock(name="EngagementRecord")
        else:
            eng = None
        self._etok = tools_mod.engagement_var.set(eng)
        return self

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._otok)
        tools_mod.engagement_var.reset(self._etok)


def _mk_hook(*, role="finance", protected=None, deps=None, deps_factory=None):
    protected = PROTECTED if protected is None else protected
    if deps_factory is None:
        def deps_factory():
            return deps
    return make_resident_authz_hook(role, protected, deps_factory)


async def _call(hook, *, tool_name=TOOL, tool_input=None):
    if tool_input is None:
        tool_input = {"amount": 10}
    return await hook({"tool_name": tool_name, "tool_input": tool_input},
                      None, {})


def _deny_reason(out):
    hso = (out or {}).get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny", out
    return hso.get("permissionDecisionReason", "")


def _expected_key(tool_input, *, role="finance", operator_id=100, chat_id=42,
                  artifact=ARTIFACT, tool=TOOL, engagement_id="") -> GrantKey:
    return GrantKey(operator_id=operator_id, chat_id=chat_id,
                    enforcement_role=role, artifact_id=artifact,
                    tool_name=tool, args_hash=canonical_args_hash(tool_input),
                    engagement_id=engagement_id)


# ---------------------------------------------------------------------------
# Coordinator / broker / channel doubles (mirror test_authz_grants.py)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.eng_dispatches: list = []
        self.post_result: int | None = 55
        self.post_raises = False
        self.post_gate: asyncio.Event | None = None
        self.dispatch_result = True
        # #368: fakes model the CONFIGURED-operator sender by default; the
        # operator-only gate tests flip this to model a stranger (or an
        # accept-all deployment, where NO sender is the operator).
        self.operator_ok = True

    def _user_id_is_operator(self, user_id) -> bool:
        return self.operator_ok

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        if self.post_gate is not None:
            await self.post_gate.wait()
        if self.post_raises:
            raise RuntimeError("post boom")
        return self.post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_button_continuation(self, **kw):
        self.dispatches.append(kw)
        return self.dispatch_result

    async def _dispatch_engagement_continuation(self, **kw):
        self.eng_dispatches.append(kw)
        return self.dispatch_result


def _fresh_coord(monkeypatch, *, ttl=None):
    """A fresh real broker (monkeypatched into the singleton slot the
    coordinator resolves at call time), a fresh coordinator, a fake channel."""
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    if ttl is not None:
        monkeypatch.setattr(authz_grants, "_CHALLENGE_TTL_S", ttl)
    return broker, ChallengeCoordinator(), _FakeChannel()


async def _settle(n: int = 6):
    for _ in range(n):
        await asyncio.sleep(0)


class _ExplodingStore:
    """A grant store whose every method raises — proves the passthrough path
    never touches it, and stands in for the injected store-stage failure."""

    def consume(self, key):
        raise AssertionError("store must not be consulted here")

    def mint(self, key, **kw):
        raise AssertionError("store must not be minted here")


class _ExplodingCoord:
    def get_or_create(self, *a, **kw):
        raise AssertionError("coordinator must not be consulted here")


# ===========================================================================
# Passthrough
# ===========================================================================


class TestPassthrough:
    async def test_unprotected_tool_returns_empty_no_deps_no_store_touch(self):
        called = {"factory": False}

        def factory():
            called["factory"] = True
            return AuthzDeps(channel=_FakeChannel(), grants=_ExplodingStore(),
                             challenges=_ExplodingCoord())

        hook = _mk_hook(deps_factory=factory)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_name="Read", tool_input={"x": 1})
        assert out == {}
        assert called["factory"] is False  # deps never resolved for passthrough


# ===========================================================================
# Provenance gates (before any grant lookup)
# ===========================================================================


class TestProvenanceGates:
    async def test_non_specialist_engagement_denies_unavailable(self):
        """#400: an engagement record that is NOT an authorizable active
        specialist (here a bare Mock, kind != "specialist") denies with the
        unavailable reason and NO challenge — and with NO closure-role assertion
        even when execution_role mismatches the hook's role."""
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(role="finance", deps=deps)
        with _OriginCtx(_origin(execution_role="butler"), engaged=True):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_ENGAGEMENT_UNAVAILABLE
        assert channel.posts == []  # no challenge

    async def test_other_transport_denies_without_challenge(self):
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        # webhook-style turn: not a telegram channel_in
        with _OriginCtx(_origin(message_type="webhook", source="webhook")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_UNSUPPORTED_ORIGIN
        assert channel.posts == []

    async def test_role_mismatch_explicit_deny(self):
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        # dm/direct origin (role==execution_role==butler) but the hook's closure
        # role is finance — an explicit deny, not an assert.
        hook = _mk_hook(role="finance", deps=deps)
        with _OriginCtx(_origin(role="butler", execution_role="butler")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_ROLE_MISMATCH
        assert channel.posts == []

    async def test_no_channel_reachable_denies_unsupported_origin(self):
        """deps_factory returning None (no DM) ⇒ the unsupported-origin deny."""
        hook = _mk_hook(deps_factory=lambda: None)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_UNSUPPORTED_ORIGIN

    async def test_provenance_checked_before_consume_grant_intact(self):
        """A webhook turn with a LIVE grant consumes NOTHING (provenance denies
        first) — the grant remains consumable afterward (B4)."""
        store = GrantStore()
        ti = {"amount": 10}
        key = _expected_key(ti)
        store.mint(key)
        # spy: if the hook ever calls consume, it would spend the grant.
        deps = AuthzDeps(channel=_FakeChannel(), grants=store,
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(message_type="webhook", source="webhook")):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_UNSUPPORTED_ORIGIN
        assert store.consume(key) is True  # grant untouched by the hook


# ===========================================================================
# Operator-only authorization (#368)
# ===========================================================================


class TestOperatorOnlyAuthorization:
    """#368: an authorization challenge is answerable only by the configured
    operator, so for any other sender the hook denies OUTRIGHT — no challenge
    is posted (the requester must never approve their own protected call) and
    no grant is consumed."""

    async def test_non_operator_sender_is_denied_with_no_challenge(self):
        channel = _FakeChannel()
        channel.operator_ok = False
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_NOT_OPERATOR
        assert channel.posts == []  # no self-approvable challenge
        # #221 discipline: instruction, not UI description, and nothing
        # about the user to paraphrase back at them.
        assert "the user" not in _DENY_NOT_OPERATOR

    async def test_accept_all_mode_denies_protected_tools_for_everyone(self):
        """With telegram_chat_id empty ("accept all chats") NO sender is the
        operator — `_user_id_is_operator` is False for every id — so protected
        tools are always denied. Same mechanics as the stranger case; pinned
        separately because it is the deliberate #368 behavior change for
        accept-all deployments."""
        channel = _FakeChannel()
        channel.operator_ok = False  # empty telegram_chat_id: nobody matches
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(user_id=999, chat_id="999")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_NOT_OPERATOR
        assert channel.posts == []

    async def test_non_operator_with_live_grant_consumes_nothing(self):
        """The operator gate runs BEFORE consume: a non-operator turn with a
        LIVE grant for the identical call spends nothing (the grant remains
        consumable afterward)."""
        store = GrantStore()
        ti = {"amount": 10}
        key = _expected_key(ti)
        store.mint(key)
        channel = _FakeChannel()
        channel.operator_ok = False
        deps = AuthzDeps(channel=channel, grants=store,
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_NOT_OPERATOR
        assert store.consume(key) is True  # grant untouched by the hook

    async def test_operator_sender_still_reaches_consume(self):
        """The gate passes the configured operator through unchanged: a live
        grant is consumed and the call allowed."""
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti))
        channel = _FakeChannel()  # operator_ok defaults True
        deps = AuthzDeps(channel=channel, grants=store,
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert out == {}

    async def test_channel_without_operator_check_fails_closed(self):
        """A channel object that cannot answer the operator question denies —
        the gate must fail closed, never pass by default."""
        class _NoOperatorMethodChannel:
            posts: list = []

        deps = AuthzDeps(channel=_NoOperatorMethodChannel(),
                         grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_NOT_OPERATOR


# ===========================================================================
# Consume / challenge behaviour
# ===========================================================================


class TestConsumeAndChallenge:
    async def test_happy_path_challenge_posted_once_deny_names_button(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        reason = _deny_reason(out)
        assert reason == _DENY_POSTED
        # #221: the deny is an INSTRUCTION to the model, not a description of
        # the operator's UI. It must carry the retry contract…
        assert "retry the SAME call" in reason
        # …and it must tell the model to stay silent, because the old
        # UI-describing wording got paraphrased straight back to the operator
        # in violation of the §Protected tools no-narration rule.
        assert "NO user-facing narration" in reason
        # The guard is the BOUNDARY, not one banned phrase: any statement
        # ABOUT THE USER is relayable ("posted to the user" and "delivered to
        # the user" are equally paraphrasable), so the deny must say nothing
        # about them at all.
        assert "the user" not in reason
        assert len(channel.posts) == 1  # exactly one keyboard

    async def test_pending_dedup_second_call_one_keyboard(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out1 = await _call(hook)
            out2 = await _call(hook)
        assert _deny_reason(out1) == _DENY_POSTED
        assert _deny_reason(out2) == _DENY_PENDING
        # #221: the dedup deny is an instruction too — the keyboard is already
        # in front of the operator, so the model must not narrate it either,
        # and the same say-nothing-about-them boundary applies.
        assert "NO user-facing narration" in _deny_reason(out2)
        assert "waiting for" not in _deny_reason(out2)
        assert "the user" not in _deny_reason(out2)
        assert len(channel.posts) == 1  # ONE keyboard for both calls

    async def test_mint_then_identical_call_allows(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert out == {}  # allow
        assert channel.posts == []

    async def test_third_call_rechallenges_single_use(self, monkeypatch):
        """mint → 1st identical call allows; 2nd identical call (grant spent) →
        re-challenge (single-use).

        Pins INV-PLUG-004. Red case demonstrated: dropping the `grant.used`
        half of GrantStore.consume's check fails this test.
        """
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            first = await _call(hook, tool_input=ti)
            second = await _call(hook, tool_input=ti)
        assert first == {}
        assert _deny_reason(second) == _DENY_POSTED
        assert len(channel.posts) == 1  # the re-challenge

    async def test_args_drift_rechallenges(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        store.mint(_expected_key({"amount": 10}))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input={"amount": 999})  # drift
        assert _deny_reason(out) == _DENY_POSTED
        assert len(channel.posts) == 1

    async def test_enforcement_role_mismatch_cannot_consume(self, monkeypatch):
        """Two roles, same plugin+args: a grant minted for finance cannot be
        consumed by butler — its key's enforcement_role differs (B5)."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti, role="finance"))  # minted for finance
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(role="butler", deps=deps)  # butler tries to consume
        with _OriginCtx(_origin(role="butler", execution_role="butler")):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED  # no consume ⇒ challenge

    async def test_artifact_mismatch_cannot_consume(self, monkeypatch):
        """A grant minted against a stale artifact_id cannot be consumed after a
        plugin update changes the resolved artifact."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti, artifact="OLD-artifact"))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        # hook's protected map names the NEW artifact.
        hook = _mk_hook(
            protected={TOOL: {"artifact_id": ARTIFACT, "summary": None}},
            deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED

    async def test_restart_loses_grants_fresh_store_rechallenges(
        self, monkeypatch,
    ):
        """Pins INV-PLUG-005. Red case demonstrated: backing GrantStore._grants
        with a module-level dict shared across instances (persistence stand-in)
        fails this test."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        old_store = GrantStore()
        ti = {"amount": 10}
        old_store.mint(_expected_key(ti))
        # a fresh process ⇒ a brand-new empty store; the grant is gone.
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED
        assert old_store.consume(_expected_key(ti)) is True  # old one still had it

    async def test_delegated_target_role_is_originating_resident(
        self, monkeypatch,
    ):
        """A delegated specialist's challenge routes back to the ORIGINATING
        resident (origin.role, e.g. Ellen), not the enforcement role."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(role="finance", deps=deps)
        # origin.role = assistant (Ellen), execution_role = finance ⇒ delegated.
        with _OriginCtx(_origin(role="assistant", execution_role="finance")):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_POSTED
        # the broker record's meta names Ellen as the continuation target.
        ((chat_id, rid, _text, _opts),) = channel.posts
        meta = broker.get_meta(namespace="resident_ask",
                               scope=f"authz:{chat_id}", request_id=rid)
        assert meta["target_role"] == "assistant"
        assert meta["enforcement_role"] == "finance"

    async def test_hook_threads_summary_and_display_name_end_to_end(
        self, monkeypatch,
    ):
        """The summary travels: hook reads it from ``protected_map`` next to
        ``artifact_id`` → passes into get_or_create → coordinator renders. The
        posted challenge carries the interpolated sentence AND the verbatim
        canonical block AND the full tool id."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord,
                         display_name="Alex")
        protected = {TOOL: {"artifact_id": ARTIFACT,
                            "summary": "Delete the invoice draft for {period}"}}
        hook = _mk_hook(deps=deps, protected=protected)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input={"period": "2025-05"})
        assert _deny_reason(out) == _DENY_POSTED
        ((_chat_id, _rid, text, _opts),) = channel.posts
        assert (
            "Alex (finance) wants to: Delete the invoice draft for 2025-05"
        ) in text
        assert '```\n{"period":"2025-05"}\n```' in text  # verbatim binding
        assert f"Tool id: {TOOL}" in text


# ===========================================================================
# Unrenderable / oversized args
# ===========================================================================


class TestUnrenderable:
    async def test_unserializable_args_deny_without_challenge(self):
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input={"bad": {1, 2, 3}})  # a set
        assert _deny_reason(out) == _DENY_UNRENDERABLE
        assert channel.posts == []  # no coordinator/keyboard touched

    async def test_args_too_large_refusal_denies_no_keyboard(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        big = {"blob": "x" * 5000}  # rendered challenge exceeds the 3900 ceiling
        with _OriginCtx(_origin()):
            out = await _call(hook, tool_input=big)
        assert _deny_reason(out) == _DENY_UNRENDERABLE
        assert channel.posts == []  # refused before any post


# ===========================================================================
# settled_post outcomes (driven end-to-end through the real coordinator)
# ===========================================================================


class TestSettledPostOutcomes:
    async def test_delivery_failed_deny(self, monkeypatch):
        broker, coord, channel = _fresh_coord(monkeypatch)
        channel.post_raises = True  # the keyboard post fails
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_DELIVERY_FAILED

    async def test_inactive_timeout_while_posting_deny(self, monkeypatch):
        """The TTL fires while the post is still blocked in flight — even though
        the post then succeeds, the button is already expired (inactive)."""
        broker, coord, channel = _fresh_coord(monkeypatch, ttl=0.02)
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            task = asyncio.ensure_future(_call(hook))
            await asyncio.sleep(0)      # hook runs; post blocks on the gate
            await asyncio.sleep(0.05)   # the 0.02s TTL fires while blocked
            channel.post_gate.set()     # post now completes (returns a mid)
            out = await task
        assert _deny_reason(out) == _DENY_INACTIVE
        await _settle()
        assert len(channel.posts) == 1

    async def test_inactive_new_while_posting_deny(self, monkeypatch):
        """/new cancels the chat's authz scope while the post is in flight."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            task = asyncio.ensure_future(_call(hook))
            await asyncio.sleep(0)      # hook runs; post blocks
            broker.cancel_scope(namespace="resident_ask", scope="authz:42",
                                reason="new_session")
            channel.post_gate.set()
            out = await task
        assert _deny_reason(out) == _DENY_INACTIVE
        await _settle()


# ===========================================================================
# Fail-closed wrapper — injected failures per stage
# ===========================================================================


class _RaisingConsumeStore(GrantStore):
    def consume(self, key):
        raise RuntimeError("store boom")


class _RaisingCoord:
    def get_or_create(self, *a, **kw):
        raise RuntimeError("coordinator boom")


class _RaisingSettleHandle:
    created = True
    refused = None

    async def settled_post(self):
        raise RuntimeError("posting boom")


class _RaisingSettleCoord:
    def get_or_create(self, *a, **kw):
        return _RaisingSettleHandle()


class TestFailClosedWrapper:
    async def test_provenance_stage_raise_denies(self, monkeypatch):
        monkeypatch.setattr(
            provenance_mod, "turn_provenance",
            Mock(side_effect=RuntimeError("prov boom")))
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_store_stage_raise_denies(self):
        deps = AuthzDeps(channel=_FakeChannel(), grants=_RaisingConsumeStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_canonicalization_stage_raise_denies(self, monkeypatch):
        # A NON-ValueError from canonicalization ⇒ the internal-error deny (a
        # ValueError is the semantic unrenderable deny, tested separately).
        monkeypatch.setattr(
            authz_grants, "canonical_args_json",
            Mock(side_effect=RuntimeError("canon boom")))
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_coordinator_stage_raise_denies(self):
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_RaisingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_posting_stage_raise_denies(self):
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_RaisingSettleCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_deps_factory_raise_denies(self):
        def factory():
            raise RuntimeError("factory boom")

        hook = _mk_hook(deps_factory=factory)
        with _OriginCtx(_origin()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_INTERNAL

    async def test_cancelled_error_propagates(self, monkeypatch):
        monkeypatch.setattr(
            provenance_mod, "turn_provenance",
            Mock(side_effect=asyncio.CancelledError()))
        deps = AuthzDeps(channel=_FakeChannel(), grants=GrantStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin()):
            with pytest.raises(asyncio.CancelledError):
                await _call(hook)


# ===========================================================================
# Wiring — residents AND specialists carry the appended matcher WITH the
# pre-existing settings guard intact; executors get NOTHING.
# ===========================================================================


def _authz_matcher(opts):
    for m in (opts.hooks or {}).get("PreToolUse", []):
        for h in getattr(m, "hooks", []):
            if getattr(h, "_casa_authz_role", None) is not None:
                return m, getattr(h, "_casa_authz_role")
    return None


def _authz_hook(opts):
    """The appended authz hook callable itself (carries ``_casa_authz_role``
    and ``_casa_authz_deps_factory`` markers)."""
    for m in (opts.hooks or {}).get("PreToolUse", []):
        for h in getattr(m, "hooks", []):
            if getattr(h, "_casa_authz_role", None) is not None:
                return h
    return None


def _has_settings_guard(opts):
    pre = (opts.hooks or {}).get("PreToolUse", [])
    return any(getattr(m, "matcher", None)
               == "Write|Edit|MultiEdit|NotebookEdit|Bash" for m in pre)


_PROTECTED_MANIFEST = {"casa": {"protectedTools": ["invoice_reset"]}}


class TestWiring:
    def test_resident_options_append_authz_matcher_guard_intact(self, tmp_path):
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        # Import the resident-options test harness helpers.
        from test_agent_plugin_binding import _make_agent

        store = tmp_path / "store"
        e = entry("p", ["resident:assistant"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        a = _make_agent(tmp_path, role="assistant")

        async def run():
            return await a._build_options(
                channel="telegram", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="hi")

        opts = asyncio.run(run())
        found = _authz_matcher(opts)
        assert found is not None, "resident options missing the authz matcher"
        _matcher, role = found
        assert role == "assistant"
        assert _has_settings_guard(opts), "settings guard clobbered"

    def test_specialist_options_append_authz_matcher_guard_intact(
        self, tmp_path, monkeypatch,
    ):
        import plugin_registry
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _spec_cfg

        store = tmp_path / "store"
        e = entry("p", ["specialist:finance"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        resolution = plugin_registry.resolve_for("specialist:finance")

        opts = tools_mod._build_specialist_options(
            _spec_cfg("finance"), resolution=resolution)
        found = _authz_matcher(opts)
        assert found is not None, "specialist options missing the authz matcher"
        _matcher, role = found
        assert role == "finance"
        assert _has_settings_guard(opts), "settings guard clobbered"
        # artifact ids flow from protected_map, not an injected set.
        from plugin_grants import protected_map
        assert protected_map(resolution) == {
            "mcp__plugin_p_p__invoice_reset":
                {"artifact_id": e["artifact_id"], "summary": None}}

    def test_specialist_without_protected_tools_has_no_authz_matcher(
        self, tmp_path, monkeypatch,
    ):
        import plugin_registry
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _spec_cfg

        store = tmp_path / "store"
        e = entry("p", ["specialist:finance"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}})  # none
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        resolution = plugin_registry.resolve_for("specialist:finance")

        opts = tools_mod._build_specialist_options(
            _spec_cfg("finance"), resolution=resolution)
        assert _authz_matcher(opts) is None
        assert _has_settings_guard(opts)  # guard still present

    def test_executor_options_have_no_authz_matcher(self, tmp_path, monkeypatch):
        """Since v0.351.0 the configurator (the only executor) is
        built through the specialist builder and loads no plugin — not even
        one assigned to ``executor:configurator`` — so it has no protected
        tool and no authz matcher."""
        import agent_loader
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry

        store = tmp_path / "store"
        e = entry("p", ["executor:configurator"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)

        opts = tools_mod._build_specialist_options(agent_loader.load_configurator())
        assert opts.plugins == []
        assert _authz_matcher(opts) is None  # executors get NOTHING


# ===========================================================================
# Display-name threading through BOTH AuthzDeps factory sites (W2). The factory
# reads the CURRENT loaded cfg.character.name at call time — never a boot-time
# snapshot — so the challenge/settlement can name the agent.
# ===========================================================================


class TestDisplayNameFactoryWiring:
    def test_resident_factory_threads_current_character_name(self, tmp_path):
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _make_agent

        store = tmp_path / "store"
        e = entry("p", ["resident:assistant"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        a = _make_agent(tmp_path, role="assistant")
        a.config.character.name = "Alex"
        a._channel_manager.register(SimpleNamespace(name="telegram"))

        async def run():
            return await a._build_options(
                channel="telegram", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="hi")

        hook = _authz_hook(asyncio.run(run()))
        assert hook is not None
        deps = hook._casa_authz_deps_factory()
        assert deps is not None and deps.display_name == "Alex"

    def test_specialist_factory_threads_current_character_name(
        self, tmp_path, monkeypatch,
    ):
        import plugin_registry
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _spec_cfg
        from channels import ChannelManager

        store = tmp_path / "store"
        e = entry("p", ["specialist:finance"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        cm = ChannelManager()
        cm.register(SimpleNamespace(name="telegram"))
        monkeypatch.setattr(tools_mod, "_channel_manager", cm, raising=False)
        resolution = plugin_registry.resolve_for("specialist:finance")

        cfg = _spec_cfg("finance")
        cfg.character = SimpleNamespace(name="Alex")
        opts = tools_mod._build_specialist_options(cfg, resolution=resolution)
        hook = _authz_hook(opts)
        assert hook is not None
        deps = hook._casa_authz_deps_factory()
        assert deps is not None and deps.display_name == "Alex"

    def test_specialist_factory_without_character_degrades_not_raises(
        self, tmp_path, monkeypatch,
    ):
        """A specialist cfg lacking a ``character`` (defensive) yields
        display_name=None — the render-time guard then uses the role string;
        the factory must never raise."""
        import plugin_registry
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _spec_cfg
        from channels import ChannelManager

        store = tmp_path / "store"
        e = entry("p", ["specialist:finance"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest=_PROTECTED_MANIFEST)
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
        cm = ChannelManager()
        cm.register(SimpleNamespace(name="telegram"))
        monkeypatch.setattr(tools_mod, "_channel_manager", cm, raising=False)
        resolution = plugin_registry.resolve_for("specialist:finance")

        opts = tools_mod._build_specialist_options(
            _spec_cfg("finance"), resolution=resolution)  # no .character
        deps = _authz_hook(opts)._casa_authz_deps_factory()
        assert deps is not None and deps.display_name is None


# ===========================================================================
# #400 — engagement-backed authorization (interactive specialist engagements)
# ===========================================================================


class TestEngagementBackedAuthz:
    """#400: an operator-gated plugin tool on an INTERACTIVE SPECIALIST
    engagement routes through the DM authorization challenge, bound to THIS
    engagement — instead of being hard-denied. Identity comes from the live
    engagement record (tools.engagement_var), NOT the caller-supplied turn
    origin (which for an engagement is not a DM)."""

    async def test_active_specialist_engagement_posts_challenge(
        self, monkeypatch,
    ):
        """No grant ⇒ a DM keyboard is posted to the operator (from the record's
        origin) and the call denied with the retry contract."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        hook = _mk_hook(deps=deps)
        # NB: the turn origin here is deliberately NOT a DM — identity must come
        # from the engagement record, whose origin carries the operator DM.
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_POSTED
        assert len(channel.posts) == 1
        # posted to the operator DM chat from the RECORD origin (42), not the
        # turn origin.
        assert channel.posts[0][0] == 42

    async def test_engagement_bound_grant_allows(self, monkeypatch):
        """A grant minted under the engagement-bound key allows the retried
        call without a fresh challenge."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti, engagement_id="eng-abc123"))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement()):
            out = await _call(hook, tool_input=ti)
        assert out == {}
        assert channel.posts == []

    async def test_grant_from_other_engagement_not_consumable(self, monkeypatch):
        """The engagement-bound key means an approval minted inside engagement
        A cannot satisfy the SAME (operator, chat, role, artifact, tool, args)
        call inside a DIFFERENT engagement B — B re-challenges."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        # grant minted for engagement A
        store.mint(_expected_key(ti, engagement_id="eng-AAA"))
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        # the SAME call arriving inside engagement B
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement(id="eng-BBB")):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED  # not consumed ⇒ re-challenge
        assert len(channel.posts) == 1

    async def test_dm_grant_not_consumable_by_engagement(self, monkeypatch):
        """A grant minted on the DM/direct path (engagement_id="") cannot be
        consumed by an engagement call (engagement_id set) — the two key spaces
        never alias."""
        broker, coord, channel = _fresh_coord(monkeypatch)
        store = GrantStore()
        ti = {"amount": 10}
        store.mint(_expected_key(ti))  # DM grant, engagement_id=""
        deps = AuthzDeps(channel=channel, grants=store, challenges=coord)
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement()):
            out = await _call(hook, tool_input=ti)
        assert _deny_reason(out) == _DENY_POSTED
        assert store.consume(_expected_key(ti)) is True  # DM grant untouched

    async def test_non_operator_engagement_denies_no_challenge(self, monkeypatch):
        """#368 on the engagement path: if the record's operator is not the
        configured operator (accept-all mode, or a stranger), deny with NO
        challenge and NO post."""
        channel = _FakeChannel()
        channel.operator_ok = False
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_NOT_OPERATOR
        assert channel.posts == []

    @pytest.mark.parametrize("rec_kwargs", [
        dict(status="idle"),
        dict(status="completed"),
        dict(kind="executor"),
        dict(topic_id=None),
        dict(origin={"chat_id": "42"}),       # no user_id
        dict(origin={"user_id": 100}),        # no chat_id
        dict(origin={}),                      # no operator ids at all
        dict(id=""),                          # id-less record (would alias the
                                              # DM key space + misroute resume)
    ])
    async def test_unauthorizable_engagement_records_fail_closed(
        self, rec_kwargs,
    ):
        """Every non-authorizable specialist-engagement record shape denies with
        the unavailable reason, touches NO store/coordinator, and posts nothing.
        (An _ExplodingStore/_ExplodingCoord prove the tail is never reached.)"""
        channel = _FakeChannel()
        deps = AuthzDeps(channel=channel, grants=_ExplodingStore(),
                         challenges=_ExplodingCoord())
        hook = _mk_hook(deps=deps)
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement(**rec_kwargs)):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_ENGAGEMENT_UNAVAILABLE
        assert channel.posts == []

    async def test_engagement_no_channel_denies_unavailable(self):
        """deps_factory None (no DM reachable) on the engagement path ⇒ the
        engagement-unavailable deny (not the DM path's unsupported-origin)."""
        hook = _mk_hook(deps_factory=lambda: None)
        with _OriginCtx(_origin(message_type="other", source="internal"),
                        engagement=_engagement()):
            out = await _call(hook)
        assert _deny_reason(out) == _DENY_ENGAGEMENT_UNAVAILABLE
