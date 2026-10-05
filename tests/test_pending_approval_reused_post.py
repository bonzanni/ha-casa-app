"""#1207 under ruling #1252: a PENDING deny (an identical challenge already
registered) cuts the operator's reply only once that challenge's keyboard is
really on screen.

Two identical protected calls in one turn while the first keyboard post is
still in flight: the second call reuses the registered challenge
(``created=False``), which proves registration, not delivery. When the post
then fails, the first call is answered DELIVERY_FAILED and the model's words —
here its explanation after both results — reach the operator as they did
before the cut existed. When the post lands, both calls leave the approval
pending and the cut applies; when the challenge is retired while its post is
in flight, the first call is answered INACTIVE and the words reach the
operator.

Driven through the REAL ``Agent.handle_message`` with the REAL
``make_resident_authz_hook``, ``ChallengeCoordinator``, ``GrantStore`` and a
fresh ``VerdictBroker``; the keyboard post is held on an event, never a sleep.
"""
from __future__ import annotations

import asyncio

import pytest

from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    _DENY_DELIVERY_FAILED, _DENY_INACTIVE, _DENY_PENDING, _DENY_POSTED,
)

from test_authz_hook import _deny_reason
from test_chosen_silence_announcements import ROLE, _Factory, _make_agent
from test_pending_approval_output import (
    PROTECTED, _Channel, _Gate, _ask, _call, _drive, _msg, _result, _text,
)

pytestmark = [pytest.mark.asyncio]

EXPLAIN = "EXPLAIN-1207"


class _HeldPostChannel(_Channel):
    """The keyboard post suspends until the test settles it, then returns a
    message id (delivered) or ``None`` (the coordinator's delivery failure)."""

    def __init__(self) -> None:
        super().__init__()
        self.post_started = asyncio.Event()
        self.post_release = asyncio.Event()
        self.post_result: int | None = None
        self.delivered = 0

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, str(text), tuple(options)))
        self.post_started.set()
        await self.post_release.wait()
        if self.post_result is not None:
            self.delivered += 1
        return self.post_result


@pytest.fixture
async def held_turn(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    import tools
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    agent = _make_agent(tmp_path)
    channel = _HeldPostChannel()
    agent._channel_manager.register(channel)
    tools.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    coord = ChallengeCoordinator()
    hook = make_resident_authz_hook(
        ROLE, PROTECTED,
        lambda: AuthzDeps(channel=channel, grants=GrantStore(),
                          challenges=coord))
    yield agent, channel, hook
    await agent.aclose()


@pytest.mark.parametrize("post", ["fails", "retired", "lands"])
async def test_reused_challenge_cuts_only_a_delivered_keyboard(
        held_turn, monkeypatch, post):
    agent, channel, hook = held_turn
    gate = _Gate()

    async def _two_identical_calls():
        first = asyncio.create_task(_ask(hook, "deny-1"))
        await asyncio.wait_for(channel.post_started.wait(), 10)
        second = asyncio.create_task(_ask(hook, "deny-2"))
        # let the second call reach the reused challenge while the post hangs
        for _ in range(20):
            await asyncio.sleep(0)
        if post == "retired":
            # the challenge settles terminally (as /new does) while its post
            # is in flight: a keyboard that then lands is already dead
            import verdict_broker
            assert verdict_broker.BROKER.cancel_scope(
                namespace="resident_ask", scope=f"authz:{channel.chat_id}",
                reason="new") == 1
        channel.post_result = None if post == "fails" else 55
        channel.post_release.set()
        return await asyncio.wait_for(asyncio.gather(first, second), 10)

    script = [
        _call("deny-1"), _call("deny-2"),
        gate.step(_two_identical_calls),
        _result("deny-1"), _result("deny-2"),
        _text(EXPLAIN),
    ]
    factory = _Factory([script])

    await _drive(agent, monkeypatch, factory, _msg("dm"), gate)

    (answers,) = gate.answers
    first_reason = {"fails": _DENY_DELIVERY_FAILED, "retired": _DENY_INACTIVE,
                    "lands": _DENY_POSTED}[post]
    assert [_deny_reason(a) for a in answers] == [first_reason, _DENY_PENDING]
    assert len(channel.posts) == 1  # exactly one keyboard post attempted
    if post != "lands":
        # no live keyboard reached the operator: the model's words do, as before
        assert channel.delivered == (0 if post == "fails" else 1)
        assert channel.tokens == [EXPLAIN]
        assert channel.final_texts() == [EXPLAIN]
    else:
        assert channel.delivered == 1
        assert channel.tokens == []
        assert channel.final_texts() == []
        assert channel.turn_finished.await_count == 1
    assert channel.send.await_count == 0
    assert channel.send_response.await_count == 0
    assert len(factory.clients) == 1
