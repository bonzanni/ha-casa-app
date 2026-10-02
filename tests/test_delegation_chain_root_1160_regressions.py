"""#1160 regression pins around the chain-root capture.

The red cases (``test_delegation_chain_root_1160.py``) pin that a narration
turn records its completion's question. These pin what must NOT change with
it, each green before the fix and mutation-checked against it:

- an ordinary turn records exactly its own text — also right after a narration
  turn on the same resident, with a context carrying everything the narration
  turn's context carried plus question-shaped keys;
- the narration turn itself still sees the whole notice: its prompt, the
  ``_build_options`` input and the auto-recall query;
- an external ``/invoke`` caller cannot choose the question the origin records.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import agent as agent_mod
from bus import BusMessage, MessageType
from specialist_registry import DelegationComplete

try:
    from tests.test_agent_process import FakeSemanticMemory
    from tests.test_agent_process import _make_agent as _make_agent_with_memory
    from tests.test_notification_handling import _FakeClient, _make_agent
except ImportError:
    from test_agent_process import FakeSemanticMemory
    from test_agent_process import _make_agent as _make_agent_with_memory
    from test_notification_handling import _FakeClient, _make_agent

pytestmark = pytest.mark.asyncio

R = "ROOT_1160: reconcile the household invoice"
# Question-shaped keys a context carrier could plausibly be named; none of
# them may ever decide what the origin records.
_PLANTED = {
    "user_text": "PLANTED_QUESTION",
    "origin_question": "PLANTED_QUESTION",
    "original_user_question": "PLANTED_QUESTION",
    "_chain_root": "PLANTED_QUESTION",
    "_origin_question": "PLANTED_QUESTION",
}


class _CapturingClient(_FakeClient):
    prompts: list[str] = []
    origins: list[dict] = []

    @classmethod
    def reset(cls):
        super().reset()
        cls.prompts = []
        cls.origins = []

    async def query(self, text):
        await super().query(text)
        type(self).prompts.append(text)
        type(self).origins.append(dict(agent_mod.origin_var.get() or {}))


def _notice(result: str = "RESULT_CANARY_1") -> BusMessage:
    origin = {
        "role": "assistant", "channel": "telegram", "chat_id": "777",
        "cid": "c1", "user_text": R,
        # Markers the synthesizer copies onto the narration turn's context.
        "_origin_route": "telegram", "_origin_clearance": "private",
        "_inherited_note": "a note",
    }
    complete = DelegationComplete(
        delegation_id="d-1", agent="finance", status="ok", text=result,
        origin=origin, elapsed_s=1.0,
    )
    return BusMessage(
        type=MessageType.NOTIFICATION, source="finance", target="assistant",
        content=complete, channel="telegram",
        context={"cid": "c1", "chat_id": "777", "delegation_id": "d-1"},
    )


@pytest.mark.parametrize("kind", [MessageType.REQUEST, MessageType.SCHEDULED])
async def test_ordinary_turn_after_narration_records_its_own_text(
        tmp_path, kind):
    agent = _make_agent(tmp_path)
    _CapturingClient.reset()
    synthesized = []
    real_synth = agent._synthesize_delegation_turn

    def _synth(msg):
        out = real_synth(msg)
        synthesized.append(out)
        return out

    with patch("sdk_client_pool._default_make_client", _CapturingClient), \
            patch.object(agent, "_synthesize_delegation_turn", _synth):
        await agent.handle_message(_notice())
        # The context the narration turn left behind, plus question-shaped
        # keys: an ordinary turn built from it is still its own question.
        left_behind = {
            k: v for k, v in synthesized[0].context.items()
            if k != "_turn_scope"
        }
        await agent.handle_message(BusMessage(
            type=kind, source="telegram", target="assistant",
            content="NEW_OPERATOR_QUESTION", channel="telegram",
            context={**left_behind, **_PLANTED},
        ))

    assert len(_CapturingClient.origins) == 2
    assert _CapturingClient.origins[1]["user_text"] == "NEW_OPERATOR_QUESTION"


async def test_narration_turn_still_sees_the_whole_notice(tmp_path):
    sem = FakeSemanticMemory(overlay="O", facts="F")
    agent = _make_agent_with_memory(tmp_path, role="assistant",
                                    semantic_memory=sem)
    _CapturingClient.reset()
    built = []
    synthesized = []
    real_build = agent._build_options
    real_synth = agent._synthesize_delegation_turn

    async def _build(**kwargs):
        built.append(kwargs["user_text"])
        return await real_build(**kwargs)

    def _synth(msg):
        out = real_synth(msg)
        synthesized.append(out)
        return out

    with patch("sdk_client_pool._default_make_client", _CapturingClient), \
            patch.object(agent, "_build_options", _build), \
            patch.object(agent, "_synthesize_delegation_turn", _synth):
        await agent.handle_message(_notice("RESULT_CANARY_FULL"))

    body = synthesized[0].content
    assert "RESULT_CANARY_FULL" in body and f"was: {R}\n" in body
    assert len(_CapturingClient.prompts) == 1
    assert _CapturingClient.prompts[0].endswith(body)
    assert built == [body]
    assert [c["query"] for c in sem.recall_calls] == [body]


_SHARED_SIG_SEED = "invoke-1160-seed"


class _AgentBus:
    """The /invoke handler's bus, forwarding into the real resident."""

    def __init__(self, agent):
        self.agent = agent

    async def request(self, msg, timeout=300):
        return await self.agent.handle_message(msg)


class _Cfg:
    channels = ["telegram", "webhook"]


async def test_invoke_context_cannot_choose_the_recorded_question(tmp_path):
    from casa_core import _make_invoke_handler
    from casa_core_middleware import cid_middleware
    from rate_limit import RateLimiter

    agent = _make_agent(tmp_path)
    _CapturingClient.reset()
    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=_SHARED_SIG_SEED, bus=_AgentBus(agent),
        assistant_role="assistant", role_configs={"assistant": _Cfg()},
    )
    app = web.Application(middlewares=[cid_middleware])
    app.router.add_post("/invoke/{agent}", handler)
    body = json.dumps({
        "prompt": "INVOKE_PROMPT",
        "context": {"chat_id": "caller-1", **_PLANTED},
    }).encode()
    sig = hmac.new(_SHARED_SIG_SEED.encode(), body, hashlib.sha256).hexdigest()
    with patch("sdk_client_pool._default_make_client", _CapturingClient):
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/invoke/assistant", data=body,
                headers={"Content-Type": "application/json",
                         "X-Webhook-Signature": sig})
            assert r.status == 200

    assert len(_CapturingClient.origins) == 1
    assert _CapturingClient.origins[0]["user_text"] == "INVOKE_PROMPT"
