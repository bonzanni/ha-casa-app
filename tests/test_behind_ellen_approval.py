"""Where an approval's continuation goes once the operator answers it.

A protected call raised in a delegation behind Ellen continues to the
resident that delegated (``_dispatch_button_continuation`` to Ellen, once the
delegated agent's slot is free); one raised in a pinned stored-call turn in an
agent's own chat continues to that chat's agent. Nothing on the origin names
another destination: a ``desk`` entry in a turn's context steers nothing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import stored_call_settler as settler
from authz_grants import AuthzDeps, GrantStore, canonical_args_hash
from test_authz_grants import _fresh_env, _key, _settle, _tap

pytestmark = pytest.mark.asyncio

ELLEN_BOT, FINANCE_BOT = 111, 222


# --- the hook: which destination the challenge is raised with -------------------------

async def _raised_with(monkeypatch, origin: dict, *, endpoint_agent: str, bot: int) -> dict:
    """Run the admission hook of ``finance`` on *origin*; return the keyword
    arguments it raised the challenge with."""
    from test_authz_hook import TOOL, _FakeChannel as _HookChannel, _OriginCtx, _call, _mk_hook
    import tools
    from telegram_channel_double import make_transport
    seen: dict = {}

    async def _settled():
        return None

    class _Coord:
        def get_or_create(self, key, **kw):
            seen.update(kw)
            seen["key"] = key
            return SimpleNamespace(refused=None, created=True, settled_post=_settled,
                                   _challenge=SimpleNamespace(rid="r"))

    endpoint = _HookChannel()
    endpoint.default_agent, endpoint.bot_id, endpoint.has_bot_id = endpoint_agent, bot, True
    endpoint.chat_id = "42"
    manager = make_transport(endpoint)
    monkeypatch.setattr(tools, "_channel_manager", manager, raising=False)
    hook = _mk_hook(role="finance", deps=AuthzDeps(channel=manager.get("telegram"),
                                                   grants=GrantStore(), challenges=_Coord()))
    with _OriginCtx(origin):
        await _call(hook, tool_name=TOOL, tool_input={"amount": 10})
    return seen


async def test_a_call_raised_behind_ellen_is_raised_for_ellen(monkeypatch):
    from test_authz_hook import _origin
    origin = _origin(role="assistant", execution_role="finance", _delegation_id="d-1",
                     _origin_route="telegram", _origin_clearance="private", _tg_bot=ELLEN_BOT,
                     desk={"role": "finance", "chat_id": 42})
    seen = await _raised_with(monkeypatch, origin, endpoint_agent="assistant", bot=ELLEN_BOT)
    assert seen["target_role"] == "assistant"
    assert seen["key"].enforcement_role == "finance"
    assert "desk_role" not in seen


async def test_a_call_raised_in_a_pinned_turn_is_raised_for_the_chats_agent(monkeypatch):
    origin = settler._pinned_origin(
        resident_role="finance", executing_role="finance", bot_id=FINANCE_BOT, chat_id=42,
        user_id=100, message_id=501, cid="abc", text="[tapped: Yes]", turn_id="run-1")
    assert "desk" not in origin
    seen = await _raised_with(monkeypatch, origin, endpoint_agent="finance", bot=FINANCE_BOT)
    assert seen["target_role"] == "finance"
    assert seen["key"].enforcement_role == "finance" and seen["key"].bot_id == FINANCE_BOT
    assert "desk_role" not in seen


# --- the coordinator: where the decision is dispatched --------------------------------

def _create(coord, channel, *, target_role, chat_id=100, enforcement_role="finance"):
    key = _key(chat_id=chat_id, enforcement_role=enforcement_role,
               args_hash=canonical_args_hash({"x": 1}))
    return coord.get_or_create(
        key, chat_id=chat_id, operator_id=7, target_role=target_role,
        tool_name="invoice_reset", canonical_json='{"amount":10,"id":"INV-1"}',
        enforcement_role=enforcement_role, channel=channel, grants=GrantStore())


async def _answer(broker, handle, idx):
    await handle.settled_post()
    _tap(broker, handle._challenge, idx)
    await _settle(12)


async def test_an_approval_behind_ellen_continues_to_ellen_once_the_slot_is_free(monkeypatch):
    broker, coord, channel = _fresh_env(monkeypatch)
    waits = []

    async def _wait(chat_id, role):
        waits.append((chat_id, role))
    monkeypatch.setattr(coord, "_wait_for_slot", _wait)
    handle = _create(coord, channel, target_role="assistant")
    await _answer(broker, handle, 0)
    assert waits == [(100, "finance")]
    (d,) = channel.dispatches
    assert (d["chat_id"], d["user_id"], d["target_role"], d["request_id"]) == (
        100, 7, "assistant", handle._challenge.rid)
    assert d["text"].startswith("[authorization approved]")
    assert "✅ Approved" in channel.edits[-1][2]


async def test_a_denial_behind_ellen_is_told_to_ellen(monkeypatch):
    broker, coord, channel = _fresh_env(monkeypatch)

    async def _wait(chat_id, role):
        return None
    monkeypatch.setattr(coord, "_wait_for_slot", _wait)
    handle = _create(coord, channel, target_role="assistant")
    await _answer(broker, handle, 1)
    (d,) = channel.dispatches
    assert d["target_role"] == "assistant" and d["text"].startswith("[authorization denied]")


async def test_an_approval_in_an_agents_own_chat_continues_to_that_agent_at_once(monkeypatch):
    broker, coord, channel = _fresh_env(monkeypatch)
    waits = []

    async def _wait(chat_id, role):
        waits.append((chat_id, role))
    monkeypatch.setattr(coord, "_wait_for_slot", _wait)
    handle = _create(coord, channel, target_role="finance")
    await _answer(broker, handle, 0)
    assert waits == []                       # its own turn: nothing to wait for
    (d,) = channel.dispatches
    assert d["target_role"] == "finance" and d["text"].startswith("[authorization approved]")
