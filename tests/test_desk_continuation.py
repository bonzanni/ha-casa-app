"""S4 §7: an approval raised inside a desk turn continues the DESK, not the
resident — through a field of its own (`desk_role`) carried on the challenge
record, read at SETTLE time so a pending challenge reused from a desk is
promoted to it; the resident's `target_role` and the grant key are untouched.
"""
from __future__ import annotations

import pytest

from authz_grants import GrantStore
from test_authz_grants import _FakeChannel, _fresh_env, _key, _settle, _tap
from authz_grants import canonical_args_hash

pytestmark = pytest.mark.asyncio


class _DeskChannel(_FakeChannel):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.desk_dispatches: list[dict] = []

    async def _dispatch_desk_continuation(self, *, chat_id, user_id, desk_role, request_id, text):
        self.desk_dispatches.append(dict(chat_id=chat_id, user_id=user_id, desk_role=desk_role,
                                         request_id=request_id, text=text))
        self.log.append(("desk-dispatch", text))
        return self.dispatch_result


def _create(coord, channel, key=None, *, chat_id=100, operator_id=7,
            target_role="finance-full", tool_name="invoice_reset",
            canonical_json='{"amount":10,"id":"INV-1"}', enforcement_role="finance",
            engagement_id="", grants=None, desk_role=""):
    if key is None:
        key = _key(chat_id=chat_id, enforcement_role=enforcement_role, tool_name=tool_name,
                   args_hash=canonical_args_hash({"x": 1}), engagement_id=engagement_id)
    handle = coord.get_or_create(
        key, chat_id=chat_id, operator_id=operator_id, target_role=target_role,
        tool_name=tool_name, canonical_json=canonical_json, enforcement_role=enforcement_role,
        channel=channel, engagement_id=engagement_id,
        grants=GrantStore() if grants is None else grants, desk_role=desk_role)
    return key, handle


def _env(monkeypatch):
    broker, coord, _ = _fresh_env(monkeypatch)
    return broker, coord, _DeskChannel()


async def _approve(broker, coord, handle):
    await handle.settled_post()
    ch = handle._challenge
    _tap(broker, ch, 0)
    await _settle(12)


async def test_a_challenge_raised_from_a_desk_continues_the_desk(monkeypatch):
    broker, coord, channel = _env(monkeypatch)
    grants = GrantStore()
    key, handle = _create(coord, channel, target_role="assistant", grants=grants, desk_role="finance")
    assert handle.created
    await _approve(broker, coord, handle)
    assert channel.dispatches == []
    (d,) = channel.desk_dispatches
    assert (d["chat_id"], d["user_id"], d["desk_role"], d["request_id"]) == (100, 7, "finance", handle._challenge.rid)
    assert d["text"].startswith("[authorization approved]")
    assert "✅ Approved" in channel.edits[-1][2]


async def test_a_challenge_without_a_desk_continues_the_resident_as_today(monkeypatch):
    broker, coord, channel = _env(monkeypatch)
    key, handle = _create(coord, channel, target_role="finance", grants=GrantStore())
    await _approve(broker, coord, handle)
    assert channel.desk_dispatches == []
    (d,) = channel.dispatches
    assert d["target_role"] == "finance"


async def test_a_pending_challenge_reused_from_a_desk_is_promoted_to_it(monkeypatch):
    """The resident's delegation raised it; the desk raised the identical
    call while it was pending: the ONE approval continues the desk."""
    broker, coord, channel = _env(monkeypatch)
    grants = GrantStore()
    key, first = _create(coord, channel, target_role="assistant", grants=grants)
    assert first.created
    key2, second = _create(coord, channel, key=key, target_role="assistant", grants=grants,
                           desk_role="finance")
    assert not second.created and second._challenge is first._challenge
    await _approve(broker, coord, first)
    assert channel.dispatches == []
    assert [d["desk_role"] for d in channel.desk_dispatches] == ["finance"]
    assert len(channel.posts) == 1                              # one keyboard, as before


async def test_a_denied_desk_challenge_tells_the_desk_too(monkeypatch):
    broker, coord, channel = _env(monkeypatch)
    key, handle = _create(coord, channel, target_role="assistant", grants=GrantStore(),
                          desk_role="finance")
    await handle.settled_post()
    _tap(broker, handle._challenge, 1)
    await _settle(12)
    (d,) = channel.desk_dispatches
    assert d["text"].startswith("[authorization denied]")


async def test_the_hook_threads_the_identitys_desk_role_into_the_challenge(monkeypatch):
    """The admission hook raises the challenge with the identity's advisory
    desk_role, so a desk turn's protected call is routed back to the desk."""
    from types import SimpleNamespace
    from authz_grants import AuthzDeps
    from test_authz_hook import TOOL, _FakeChannel as _HookChannel, _OriginCtx, _call, _mk_hook, _origin
    seen = {}

    async def _settled():
        return None

    class _Coord:
        def get_or_create(self, key, **kw):
            seen.update(kw)
            seen["key"] = key
            return SimpleNamespace(refused=None, created=True, settled_post=_settled,
                                   _challenge=SimpleNamespace(rid="r"))

    hook = _mk_hook(role="finance", deps=AuthzDeps(channel=_HookChannel(), grants=GrantStore(),
                                                   challenges=_Coord()))
    desk_origin = _origin(role="assistant", execution_role="finance", _delegation_id="d-1",
                          desk={"role": "finance", "chat_id": 42})
    with _OriginCtx(desk_origin):
        await _call(hook, tool_name=TOOL, tool_input={"amount": 10})
    assert seen["desk_role"] == "finance" and seen["target_role"] == "assistant"
    assert seen["key"].enforcement_role == "finance"


async def test_a_desk_continuation_never_waits_for_the_residents_slot(monkeypatch):
    broker, coord, channel = _env(monkeypatch)
    waits = []

    async def _wait(chat_id, role):
        waits.append((chat_id, role))
    monkeypatch.setattr(coord, "_wait_for_slot", _wait)
    key, handle = _create(coord, channel, target_role="assistant", grants=GrantStore(),
                          desk_role="finance")
    await _approve(broker, coord, handle)
    assert waits == [] and len(channel.desk_dispatches) == 1
    # the resident's own delegated challenge still waits for the slot, as today
    key2, handle2 = _create(coord, channel, key=None, chat_id=101, target_role="assistant",
                            grants=GrantStore())
    await _approve(broker, coord, handle2)
    assert waits == [(101, "finance")]
