"""Tests for engagement_permission_relay PreToolUse hook (v0.75.0: migrated
onto verdict_broker.BROKER — W5/Sol B3,B4).
"""

from __future__ import annotations

import asyncio

import pytest
from broker_helpers import deliver

import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    """Isolate every test on its own VerdictBroker — hooks.py resolves
    ``from verdict_broker import BROKER`` at call time, so redirecting the
    module attribute here is picked up transparently."""
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


class _FakeRecord:
    def __init__(self, status="active", tools_allowed=(),
                 permission_mode="acceptEdits", topic_id=555,
                 origin=None):
        self.status = status
        self.tools_allowed = tuple(tools_allowed)
        self.permission_mode = permission_mode
        self.topic_id = topic_id
        self.origin = origin if origin is not None else {"user_id": 999}


class _FakeRegistry:
    def __init__(self, records: dict | None = None):
        self._records = records or {}

    def get(self, eid):
        return self._records.get(eid)


class _FakeTelegramChannel:
    def __init__(self, *, next_message_id=555, post_raises=None,
                 post_returns_none=False, operator_id=999):
        self.state_calls = []
        self.keyboard_calls = []
        self.edit_calls = []
        self._next_message_id = next_message_id
        self._post_raises = post_raises
        self._post_returns_none = post_returns_none
        self._operator_id = operator_id

    def operator_user_id(self):
        return self._operator_id

    async def update_topic_state(self, *, engagement_id, new_state):
        self.state_calls.append((engagement_id, new_state))

    async def post_perm_keyboard(self, **kw):
        self.keyboard_calls.append(kw)
        if self._post_raises is not None:
            raise self._post_raises
        if self._post_returns_none:
            return None
        return self._next_message_id

    async def edit_perm_keyboard_outcome(self, *, topic_id, message_id, outcome):
        self.edit_calls.append((topic_id, message_id, dict(outcome)))


class TestUnknownContext:
    async def test_no_authenticated_identity_denies(self):
        """#366 red case: an engagement-workspace cwd claim with NO
        authenticated identity (empty context) is exactly the pre-fix
        forgery — the relay must deny without consulting the registry."""
        from hooks import make_engagement_permission_relay
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry(),
            telegram_channel=_FakeTelegramChannel(),
        )
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": "/data/engagements/" + "a" * 32},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "not authenticated" in _reason(result)

    async def test_non_dict_context_denies(self):
        """A non-dict context (any exotic invoker) carries no authenticated
        identity — fail closed, never crash."""
        from hooks import make_engagement_permission_relay
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry(),
            telegram_channel=_FakeTelegramChannel(),
        )
        result = await hook(
            {"tool_name": "Read", "tool_input": {}, "cwd": "/etc"},
            None, None,
        )
        assert _decision(result) == "deny"
        assert "not authenticated" in _reason(result)

    async def test_cwd_is_never_an_identity_source(self):
        """#366: identity comes from the authenticated context even when the
        payload cwd names a DIFFERENT engagement (the handler cross-checks
        upstream; the relay itself must bind to the credential)."""
        from hooks import make_engagement_permission_relay
        eid = "c" * 32
        reg = _FakeRegistry({eid: _FakeRecord(tools_allowed=("Read",))})
        hook = make_engagement_permission_relay(
            engagement_registry=reg,
            telegram_channel=_FakeTelegramChannel(),
        )
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": "/data/engagements/" + "f" * 32},
            None, {"casa_engagement_id": eid},
        )
        assert result == {}  # bound to eid's record, allow-listed Read


class TestEngagementResolution:
    async def test_engagement_not_in_registry(self):
        from hooks import make_engagement_permission_relay
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry(),
            telegram_channel=_FakeTelegramChannel(),
        )
        eid = "a" * 32
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": f"/data/engagements/{eid}"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "unknown or inactive" in _reason(result)

    async def test_inactive_engagement(self):
        from hooks import make_engagement_permission_relay
        eid = "b" * 32
        reg = _FakeRegistry({eid: _FakeRecord(status="completed")})
        hook = make_engagement_permission_relay(
            engagement_registry=reg,
            telegram_channel=_FakeTelegramChannel(),
        )
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": f"/data/engagements/{eid}"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "unknown or inactive" in _reason(result)

    async def test_cwd_subdir_resolves_and_allow_listed(self):
        from hooks import make_engagement_permission_relay
        eid = "c" * 32
        reg = _FakeRegistry({eid: _FakeRecord(tools_allowed=("Read",))})
        hook = make_engagement_permission_relay(
            engagement_registry=reg,
            telegram_channel=_FakeTelegramChannel(),
        )
        # cwd is a sub-directory of the engagement workspace — should still resolve.
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": f"/data/engagements/{eid}/src"},
            None, {"casa_engagement_id": eid},
        )
        # tools_allowed=("Read",) so it should pass-through
        assert result == {}


class TestPermissionModeShortCircuit:
    """G-1 v0.37.7: executors with permission_mode={auto,bypassPermissions}
    bypass the relay hook so autonomous (no-operator-at-keyboard) runs work.
    acceptEdits and default still fall through to the allow-list + relay
    pipeline.
    """

    async def test_auto_short_circuits_without_keyboard(self):
        from hooks import make_engagement_permission_relay
        eid = "a" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),  # empty -> would normally relay
                             permission_mode="auto"),
        })
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=0.01,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_auto"},
            None, {"casa_engagement_id": eid},
        )
        assert result == {}
        # No keyboard ever posted, no state transitions emitted.
        assert tg.keyboard_calls == []
        assert tg.state_calls == []

    async def test_bypass_permissions_short_circuits(self):
        from hooks import make_engagement_permission_relay
        eid = "b" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),
                             permission_mode="bypassPermissions"),
        })
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=0.01,
        )
        result = await hook(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/tmp/x", "content": "y"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_bp"},
            None, {"casa_engagement_id": eid},
        )
        assert result == {}
        assert tg.keyboard_calls == []
        assert tg.state_calls == []

    async def test_accept_edits_falls_through_to_relay(self):
        from hooks import make_engagement_permission_relay
        eid = "c" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),  # nothing allow-listed
                             permission_mode="acceptEdits"),
        })
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=0.05,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_ae"},
            None, {"casa_engagement_id": eid},
        )
        # acceptEdits MUST take the relay path: keyboard posted, then
        # operator-timeout deny lands.
        assert _decision(result) == "deny"
        assert "did not respond" in _reason(result)
        assert len(tg.keyboard_calls) == 1
        assert tg.state_calls[0] == (eid, "awaiting")
        assert tg.state_calls[-1] == (eid, "active")

    async def test_default_mode_falls_through_to_relay(self):
        from hooks import make_engagement_permission_relay
        eid = "d" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),
                             permission_mode="default"),
        })
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=0.05,
        )
        result = await hook(
            {"tool_name": "Edit",
             "tool_input": {"file_path": "/x", "old_string": "a",
                            "new_string": "b"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_def"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "did not respond" in _reason(result)
        assert len(tg.keyboard_calls) == 1

    async def test_missing_permission_mode_defaults_to_relay(self):
        """Defensive: records loaded from a pre-v0.37.7 tombstone may not
        carry permission_mode. The hook must default such records to the
        relay path (current behaviour), NOT silently bypass.
        """
        from hooks import make_engagement_permission_relay
        eid = "e" * 32

        class _OldRecord:
            status = "active"
            tools_allowed = ()
            topic_id = 555
            origin = {"user_id": 1}
            # No permission_mode attribute at all.

        reg = _FakeRegistry({eid: _OldRecord()})
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=0.05,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_old"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "did not respond" in _reason(result)


class TestOperatorBinding:
    """#374/#469: the keyboard's answerer is the CONFIGURED operator, not the
    engagement creator; no configured operator means an immediate deny."""

    async def test_binds_configured_operator_not_creator(self, _fresh_broker):
        """#374 red case: creator 111 started the engagement, but the
        configured operator is 999 — the broker request must bind 999, so a
        tap from the creator can never win the claim."""
        from hooks import make_engagement_permission_relay
        eid = "a1" * 16
        reg = _FakeRegistry({eid: _FakeRecord(origin={"user_id": 111})})
        tg = _FakeTelegramChannel(operator_id=999)
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        task = asyncio.create_task(hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}", "tool_use_id": "tuse_op"},
            None, {"casa_engagement_id": eid},
        ))
        await asyncio.sleep(0.05)
        meta = _fresh_broker.get_meta(
            namespace="permission", scope=eid, request_id="tuse_op")
        assert meta["operator_id"] == 999
        # The creator's tap is forbidden; the configured operator's wins.
        assert _fresh_broker.claim(
            namespace="permission", scope=eid, request_id="tuse_op",
            option_index=0, actor_id=111) == "forbidden"
        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="tuse_op",
            option_index=0, actor_id=999) == "delivered"
        assert await asyncio.wait_for(task, timeout=1.0) == {}

    async def test_no_configured_operator_denies_without_keyboard(self):
        """Accept-all mode (#336: nobody is the operator) — the relay denies
        deterministically and immediately: no keyboard, no broker request, no
        timeout_s hold on the engagement."""
        import verdict_broker as vb
        from hooks import make_engagement_permission_relay
        eid = "b2" * 16
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel(operator_id=None)
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=600.0,
        )
        result = await asyncio.wait_for(hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}", "tool_use_id": "tuse_nop"},
            None, {"casa_engagement_id": eid},
        ), timeout=1.0)
        assert _decision(result) == "deny"
        assert "no configured operator" in _reason(result)
        assert tg.keyboard_calls == []
        assert vb.BROKER.pending(namespace="permission", scope=eid) == []


class TestBrokerVerdictRelay:
    """v0.75.0: the relay registers on verdict_broker.BROKER and awaits the
    result — the operator's Telegram tap (claim+commit) resolves it."""

    async def test_allow_via_broker(self, _fresh_broker):
        from hooks import make_engagement_permission_relay
        eid = "d" * 32
        reg = _FakeRegistry({eid: _FakeRecord(tools_allowed=())})
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        task = asyncio.create_task(hook(
            {"tool_name": "Bash",
             "tool_input": {"command": "curl example.com"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_12345"},
            None, {"casa_engagement_id": eid},
        ))
        await asyncio.sleep(0.05)  # let the hook register + post
        assert deliver(_fresh_broker, 
            namespace="permission", scope=eid, request_id="tuse_12345",
            option_index=0, actor_id=999,
        ) == "delivered"
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == {}
        assert tg.state_calls == [(eid, "awaiting"), (eid, "active")]
        assert len(tg.keyboard_calls) == 1
        kw = tg.keyboard_calls[0]
        assert kw["engagement_id"] == eid
        assert kw["request_id"] == "tuse_12345"
        assert kw["tool_name"] == "Bash"

    async def test_deny_via_broker(self, _fresh_broker):
        from hooks import make_engagement_permission_relay
        eid = "e" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        task = asyncio.create_task(hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_xyz"},
            None, {"casa_engagement_id": eid},
        ))
        await asyncio.sleep(0.05)
        assert deliver(_fresh_broker, 
            namespace="permission", scope=eid, request_id="tuse_xyz",
            option_index=1, actor_id=999,
        ) == "delivered"
        result = await asyncio.wait_for(task, timeout=1.0)
        assert _decision(result) == "deny"
        assert "denied" in _reason(result)
        assert tg.state_calls == [(eid, "awaiting"), (eid, "active")]

    async def test_timeout(self):
        from hooks import make_engagement_permission_relay
        eid = "f" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=0.1,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "curl x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_T"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "did not respond" in _reason(result)
        assert tg.state_calls[-1] == (eid, "active")

    async def test_hook_cancelled_error_cancels_broker_request(self, _fresh_broker):
        """Cancelling the awaiting hook task must resolve (not strand) the
        broker request — BROKER.pending() for this scope empties."""
        from hooks import make_engagement_permission_relay
        eid = "1" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=5.0,
        )
        task = asyncio.create_task(hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "rid-cancel"},
            None, {"casa_engagement_id": eid},
        ))
        await asyncio.sleep(0.05)  # past post + into await_result
        assert _fresh_broker.pending(namespace="permission", scope=eid) == [
            "rid-cancel",
        ]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _fresh_broker.pending(namespace="permission", scope=eid) == []
        assert tg.state_calls[-1] == (eid, "active")

    async def test_post_failure_inside_setup_task_denies_and_restores_state(self):
        """Post FAILURE inside the broker-owned setup task -> waiters get
        delivery_failed -> _deny + topic restored to 'active' EVEN on this
        return path."""
        from hooks import make_engagement_permission_relay
        eid = "2" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel(post_raises=RuntimeError("network down"))
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "rid"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "keyboard post failed" in _reason(result)
        assert tg.state_calls[-1] == (eid, "active")

    async def test_post_returning_none_is_delivery_failure(self, _fresh_broker):
        """r10-B3: post_perm_keyboard returning None (unresolvable
        engagement/topic) is a delivery FAILURE too — same unregister ->
        delivery_failed -> _deny, no finish hook installed."""
        from hooks import make_engagement_permission_relay
        eid = "3" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel(post_returns_none=True)
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "rid-none"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "keyboard post failed" in _reason(result)
        assert tg.state_calls[-1] == (eid, "active")
        # No finish hook was ever set (post never yielded a message_id).
        assert _fresh_broker.pending(namespace="permission", scope=eid) == []
        assert tg.edit_calls == []

    async def test_cancellation_during_post_never_duplicates_keyboard(
        self, _fresh_broker,
    ):
        """r8-B3: cancelling the awaiting hook task WHILE the keyboard post
        is still in flight must not duplicate the post — the shielded setup
        task completes in the background, BROKER.cancel resolves the
        request, the finish-hook edits the keyboard once, and a same-id
        re-invocation reattaches (created=False) rather than reposting."""
        from hooks import make_engagement_permission_relay
        eid = "4" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()

        post_started = asyncio.Event()
        release_post = asyncio.Event()
        post_calls: list[dict] = []

        async def slow_post(**kw):
            post_calls.append(kw)
            post_started.set()
            await release_post.wait()
            return 777

        tg.post_perm_keyboard = slow_post
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=5.0,
        )
        payload = {
            "tool_name": "Bash", "tool_input": {"command": "x"},
            "cwd": f"/data/engagements/{eid}", "tool_use_id": "rid-slow",
        }

        task = asyncio.create_task(hook(payload, None, {"casa_engagement_id": eid}))
        await asyncio.wait_for(post_started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The shielded setup task is still in flight (blocked on
        # release_post). Let it complete now.
        release_post.set()
        await _fresh_broker.drain_hooks()

        assert len(post_calls) == 1
        assert tg.edit_calls == [(555, 777, {"outcome": "cancelled",
                                              "reason": "tool_invocation_cancelled"})]

        # A same-id re-invocation reattaches to the (cancelled) tombstone —
        # NOT a second post.
        result2 = await asyncio.wait_for(
            asyncio.create_task(hook(payload, None, {"casa_engagement_id": eid})), timeout=1.0,
        )
        assert len(post_calls) == 1  # still exactly one post overall
        assert _decision(result2) == "deny"  # cancelled reattach denies
        assert tg.state_calls[-1] == (eid, "active")

    async def test_no_theft_from_concurrent_engagement_ask(self, _fresh_broker):
        """A pending engagement_ask request on the same scope survives a
        concurrent permission flow untouched — different namespaces never
        cross-resolve each other."""
        from hooks import make_engagement_permission_relay
        eid = "5" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()

        ask_req, ask_created = _fresh_broker.register(
            namespace="engagement_ask", scope=eid, request_id="ask-1",
            timeout_s=10.0, meta={"marker": "untouched"},
        )
        assert ask_created is True

        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        task = asyncio.create_task(hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}", "tool_use_id": "perm-1"},
            None, {"casa_engagement_id": eid},
        ))
        await asyncio.sleep(0.05)
        assert deliver(_fresh_broker, 
            namespace="permission", scope=eid, request_id="perm-1",
            option_index=0, actor_id=999,
        ) == "delivered"
        await asyncio.wait_for(task, timeout=1.0)

        # The ask request is untouched: still pending, same meta.
        assert _fresh_broker.pending(namespace="engagement_ask", scope=eid) == [
            "ask-1",
        ]
        assert _fresh_broker.get_meta(
            namespace="engagement_ask", scope=eid, request_id="ask-1",
        ) == {"marker": "untouched"}


class TestConcurrentRelayTopicState:
    """#324: per-engagement pending count — one verdict's exit must not
    repaint 'active' while a sibling keyboard still awaits, and a cancel
    inside the initial 'awaiting' edit must not strand the topic."""

    async def test_first_verdict_keeps_awaiting_while_second_pends(
        self, _fresh_broker,
    ):
        from hooks import make_engagement_permission_relay
        eid = "a1" * 16
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        def _input(rid):
            return ({"tool_name": "Bash", "tool_input": {"command": rid},
                     "cwd": f"/data/engagements/{eid}", "tool_use_id": rid},
                    None, {"casa_engagement_id": eid})
        t1 = asyncio.create_task(hook(*_input("rid-one")))
        t2 = asyncio.create_task(hook(*_input("rid-two")))
        await asyncio.sleep(0.05)

        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="rid-one",
            option_index=0, actor_id=999,
        ) == "delivered"
        assert await asyncio.wait_for(t1, timeout=1.0) == {}
        # rid-two still awaits its keyboard: the topic must stay 'awaiting'.
        assert (eid, "active") not in tg.state_calls, tg.state_calls

        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="rid-two",
            option_index=0, actor_id=999,
        ) == "delivered"
        assert await asyncio.wait_for(t2, timeout=1.0) == {}
        assert tg.state_calls[-1] == (eid, "active")
        assert tg.state_calls.count((eid, "active")) == 1

    async def test_stale_active_repaint_corrected_when_new_relay_started(
        self, _fresh_broker,
    ):
        """Sol diff-r1 S2: relay A's last-exit 'active' edit can land AFTER a
        newly-started relay B already painted 'awaiting' — the exit path must
        recheck the tally and repaint 'awaiting'."""
        from hooks import make_engagement_permission_relay
        eid = "c3" * 16
        reg = _FakeRegistry({eid: _FakeRecord()})

        class _SlowActiveChannel(_FakeTelegramChannel):
            def __init__(self):
                super().__init__()
                self.release_active = asyncio.Event()
                self._blocked_once = False

            async def update_topic_state(self, *, engagement_id, new_state):
                if new_state == "active" and not self._blocked_once:
                    self._blocked_once = True
                    await self.release_active.wait()
                self.state_calls.append((engagement_id, new_state))

        tg = _SlowActiveChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        def _input(rid):
            return ({"tool_name": "Bash", "tool_input": {"command": rid},
                     "cwd": f"/data/engagements/{eid}", "tool_use_id": rid},
                    None, {"casa_engagement_id": eid})
        t1 = asyncio.create_task(hook(*_input("rid-A")))
        await asyncio.sleep(0.05)
        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="rid-A",
            option_index=0, actor_id=999,
        ) == "delivered"
        await asyncio.sleep(0.05)   # t1 parked inside its 'active' edit

        t2 = asyncio.create_task(hook(*_input("rid-B")))
        await asyncio.sleep(0.05)   # t2 painted 'awaiting', now awaits verdict
        tg.release_active.set()     # A's stale 'active' lands last
        assert await asyncio.wait_for(t1, timeout=1.0) == {}
        await asyncio.sleep(0.05)

        # The topic must not read 'active' while rid-B's keyboard is live.
        assert tg.state_calls[-1] == (eid, "awaiting"), tg.state_calls

        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="rid-B",
            option_index=0, actor_id=999,
        ) == "delivered"
        assert await asyncio.wait_for(t2, timeout=1.0) == {}
        assert tg.state_calls[-1] == (eid, "active")

    async def test_delayed_awaiting_paint_cannot_outlive_the_last_active(
        self, _fresh_broker,
    ):
        """Sol diff-r2 S2: an 'awaiting' paint delayed in flight must not land
        after the LAST relay's final 'active' repaint (a stranded-awaiting
        topic with no live keyboard). State edits are serialized so the final
        paint always reflects the final tally."""
        from hooks import make_engagement_permission_relay
        eid = "d4" * 16
        reg = _FakeRegistry({eid: _FakeRecord()})

        class _ReorderingChannel(_FakeTelegramChannel):
            def __init__(self):
                super().__init__()
                self.release_active = asyncio.Event()
                self.release_awaiting3 = asyncio.Event()
                self._active_blocked_once = False
                self._awaiting_seen = 0

            async def update_topic_state(self, *, engagement_id, new_state):
                if new_state == "active" and not self._active_blocked_once:
                    self._active_blocked_once = True
                    await self.release_active.wait()
                if new_state == "awaiting":
                    self._awaiting_seen += 1
                    if self._awaiting_seen == 3:
                        await self.release_awaiting3.wait()
                self.state_calls.append((engagement_id, new_state))

        tg = _ReorderingChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=2.0,
        )
        def _input(rid):
            return ({"tool_name": "Bash", "tool_input": {"command": rid},
                     "cwd": f"/data/engagements/{eid}", "tool_use_id": rid},
                    None, {"casa_engagement_id": eid})
        t1 = asyncio.create_task(hook(*_input("rid-A")))
        await asyncio.sleep(0.05)
        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="rid-A",
            option_index=0, actor_id=999,
        ) == "delivered"
        await asyncio.sleep(0.05)     # t1 parked in its 'active' edit

        t2 = asyncio.create_task(hook(*_input("rid-B")))
        await asyncio.sleep(0.05)
        tg.release_active.set()       # A's 'active' lands; any late 'awaiting'
        await asyncio.sleep(0.05)     # for rid-B may still be in flight
        assert deliver(_fresh_broker,
            namespace="permission", scope=eid, request_id="rid-B",
            option_index=0, actor_id=999,
        ) == "delivered"
        await asyncio.sleep(0.05)     # rid-B's exit paint lands first
        tg.release_awaiting3.set()    # a delayed third paint (if any) lands last
        assert await asyncio.wait_for(t1, timeout=1.0) == {}
        assert await asyncio.wait_for(t2, timeout=1.0) == {}
        await asyncio.sleep(0.1)

        # No pending relays remain: the topic must end 'active'.
        assert tg.state_calls[-1] == (eid, "active"), tg.state_calls

    async def test_cancel_during_initial_awaiting_edit_does_not_strand(self):
        from hooks import make_engagement_permission_relay
        eid = "b2" * 16
        reg = _FakeRegistry({eid: _FakeRecord()})

        class _SlowAwaitingChannel(_FakeTelegramChannel):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()

            async def update_topic_state(self, *, engagement_id, new_state):
                if new_state == "awaiting":
                    self.entered.set()
                    await asyncio.Event().wait()   # blocks until cancelled
                self.state_calls.append((engagement_id, new_state))

        tg = _SlowAwaitingChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=5.0,
        )
        task = asyncio.create_task(hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}", "tool_use_id": "rid-strand"},
            None, {"casa_engagement_id": eid},
        ))
        await asyncio.wait_for(tg.entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The topic must not be stranded 'awaiting' — the exit path restores.
        assert (eid, "active") in tg.state_calls, tg.state_calls


class TestKeyboardFailure:
    async def test_keyboard_post_raises(self):
        from hooks import make_engagement_permission_relay
        eid = "2f" * 16

        class _BrokenTg:
            def __init__(self):
                self.state_calls = []
            def operator_user_id(self):
                return 999
            async def update_topic_state(self, *, engagement_id, new_state):
                self.state_calls.append((engagement_id, new_state))
            async def post_perm_keyboard(self, **kw):
                raise RuntimeError("network down")

        tg = _BrokenTg()
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry({eid: _FakeRecord()}),
            telegram_channel=tg, timeout_s=1.0,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "rid"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "keyboard post failed" in _reason(result)
        # State returned to active even on keyboard failure.
        assert tg.state_calls[-1] == (eid, "active")


class TestPermissionRetryReattach:
    """F1 (Sol r2): a permission RETRY (BROKER created=False) must follow the
    reattach discipline — reuse the live intent, NEVER eager-post the keyboard
    around the sequencer. Probe: no keyboard posted before any relay involvement.
    """

    async def test_retry_reattaches_without_eager_keyboard(
        self, _fresh_broker, monkeypatch,
    ):
        from hooks import make_engagement_permission_relay
        from channels.output_sequencer import OutputSequencer
        import agent as agent_mod

        eid = "7" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()

        posts: list = []
        orig_post = tg.post_perm_keyboard

        async def _counting_post(**kw):
            posts.append(kw)
            return await orig_post(**kw)

        tg.post_perm_keyboard = _counting_post

        async def _noop_send(topic, text, reply_to=None):
            return None

        async def _noop_edit(topic, mid, text):
            return True

        # A live sequencer whose ``arm`` does NOT drive the relay — the keyboard
        # is relay-DEFERRED and stays unposted here, so ANY post is an eager one.
        seq = OutputSequencer(
            engagement_id=eid, topic_id=555,
            send_message=_noop_send, edit_message=_noop_edit)

        class _Drv:
            def register_send_intent(self, *, engagement_id, request_id,
                                     tool_name, projection_hash, poster,
                                     on_retire=None):
                return seq.register_intent(
                    request_id=request_id, tool_name=tool_name,
                    projection_hash=projection_hash, poster=poster,
                    on_retire=on_retire)

            def set_send_intent_poster(self, e, r, p):
                return seq.set_intent_poster(r, p)

            def arm_send_intent(self, e, r):
                return seq.arm_intent(r)  # arm only — relay not driven

        monkeypatch.setattr(agent_mod, "active_claude_code_driver", _Drv())

        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg, timeout_s=5.0)
        payload = {
            "tool_name": "Bash", "tool_input": {"command": "x"},
            "cwd": f"/data/engagements/{eid}", "tool_use_id": "rid-retry",
        }

        # First attempt: registers + arms the intent (relay-deferred, unposted).
        t1 = asyncio.create_task(hook(payload, None, {"casa_engagement_id": eid}))
        await asyncio.sleep(0.02)
        # RETRY with the SAME id while the first still awaits → BROKER created=
        # False. Old code eager-posts the keyboard here; the fix reattaches.
        t2 = asyncio.create_task(hook(payload, None, {"casa_engagement_id": eid}))
        await asyncio.sleep(0.02)

        assert posts == []  # no eager keyboard on EITHER attempt
        # Exactly ONE intent exists for the request (reattach, not a duplicate).
        assert seq.registry.by_request_id("rid-retry") is not None

        # Resolve so both hook invocations finish.
        assert deliver(_fresh_broker, 
            namespace="permission", scope=eid, request_id="rid-retry",
            option_index=0, actor_id=999) == "delivered"
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
        assert posts == []  # still never eager-posted


class TestTerminalRegistrationSentinel:
    async def test_terminal_registration_denies_and_unregisters(
        self, _fresh_broker, monkeypatch,
    ):
        """Sol r2 (P1): terminalization landing between the hook's active
        check and intent registration makes ``register_send_intent`` return
        the ``TERMINAL_REGISTRATION`` sentinel — which is not None and not a
        tuple. The relay used to unpack it (TypeError) and strand the broker
        request until timeout. It must deny deterministically and clear the
        request."""
        import agent as agent_mod
        from channels.output_sequencer import TERMINAL_REGISTRATION
        from hooks import make_engagement_permission_relay

        eid = "t" * 32
        rec = _FakeRecord()

        class _TerminalDriver:
            def register_send_intent(self, **kw):
                # The registration observes the terminalized sequencer — by
                # this point the registry record is terminal too.
                rec.status = "completed"
                return TERMINAL_REGISTRATION

        monkeypatch.setattr(
            agent_mod, "active_claude_code_driver", _TerminalDriver(),
            raising=False)
        reg = _FakeRegistry({eid: rec})
        ch = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=ch,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "tool_use_id": "tu-terminal-1"},
            None, {"casa_engagement_id": eid},
        )
        assert _decision(result) == "deny"
        assert "terminal" in _reason(result).lower()
        # No keyboard was ever posted and no permission request is left
        # pending to burn its timeout.
        assert ch.keyboard_calls == []
        assert _fresh_broker.pending(namespace="permission", scope=eid) == []
        # Terra r3: the exit restore must NOT repaint the terminal
        # engagement's topic ``active`` — only the entry ``awaiting`` edit
        # ever ran.
        assert ch.state_calls == [(eid, "awaiting")]
