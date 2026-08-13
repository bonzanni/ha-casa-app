"""#411 — the `wipe_memory` agent door: consent-gated in code, operator-bound,
detached execution from the broker finish hook, single-flight."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import memory_wipe
import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio

WIPE_TOOL = "mcp__casa-framework__wipe_memory"


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _fresh_wipe_state(monkeypatch):
    monkeypatch.setattr(memory_wipe, "_wipe_task", None)
    monkeypatch.setattr(memory_wipe, "_wipes_frozen", False)


class _FakeChannel:
    """Operator DM double: positive chat_id makes operator_identity resolve."""

    chat_id = "500"

    def __init__(self, *, post_result=42):
        self.posts: list[tuple] = []
        self.edits: list[tuple] = []
        self._post_result = post_result

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return self._post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True


def _payload(res):
    return json.loads(res["content"][0]["text"])


def _set_origin(agent_mod, **overrides):
    origin = {
        "role": "assistant", "channel": "telegram", "chat_id": "500",
        "user_id": 500, "message_type": "channel_in", "source": "telegram",
        "execution_role": "assistant",
    }
    origin.update(overrides)
    return agent_mod.origin_var.set(origin)


async def _invoke(monkeypatch, *, channel=None, registry=None, sem=None,
                  origin_overrides=None):
    import agent as agent_mod
    import tools as tools_mod

    if channel is None:
        channel = _FakeChannel()
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    tools_mod.init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    monkeypatch.setattr(
        agent_mod, "active_session_registry",
        registry if registry is not None else MagicMock(), raising=False,
    )
    monkeypatch.setattr(
        agent_mod, "active_semantic_memory",
        sem if sem is not None else MagicMock(), raising=False,
    )
    tok = _set_origin(agent_mod, **(origin_overrides or {}))
    try:
        result = await tools_mod.wipe_memory.handler({})
    finally:
        agent_mod.origin_var.reset(tok)
    return _payload(result), channel


async def _tap(broker, channel, *, chat_id=500, actor_id=500, idx=0):
    """Commit the consent like the telegram callback route would: claim →
    commit → (finish hook fires broker-owned)."""
    rid = broker.pending(namespace="resident_ask", scope=f"authz:{chat_id}")[0]
    claim = broker.claim(
        namespace="resident_ask", scope=f"authz:{chat_id}", request_id=rid,
        option_index=idx, actor_id=actor_id,
    )
    assert not isinstance(claim, str), f"claim failed: {claim}"
    assert broker.commit(claim)
    await asyncio.sleep(0)   # let the finish-hook task run
    await asyncio.sleep(0)
    return rid


class TestGates:
    async def test_refused_from_engagement_context(self, monkeypatch):
        import tools as tools_mod
        rec = MagicMock()
        rec.context_rebuild_pending = False   # get past the #369 rebuild fence
        tok = tools_mod.engagement_var.set(rec)
        try:
            payload, _ = await _invoke(monkeypatch)
        finally:
            tools_mod.engagement_var.reset(tok)
        # The refusal is the contract; WHICH gate fires first is not — the
        # provenance gate (engagement turns are never direct DM turns)
        # normally wins, with the explicit engagement check as backstop.
        assert payload["status"] == "error"
        assert payload["kind"] in ("forbidden", "unsupported_origin")

    async def test_refused_on_non_direct_origin(self, monkeypatch):
        payload, _ = await _invoke(
            monkeypatch,
            origin_overrides={"message_type": "scheduled", "source": "scheduler"},
        )
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"

    async def test_refused_without_configured_operator(self, monkeypatch):
        class NoOperatorChannel(_FakeChannel):
            chat_id = ""   # identity v0.136: empty ⇒ NOBODY is operator

        payload, _ = await _invoke(monkeypatch, channel=NoOperatorChannel())
        assert payload["status"] == "error"
        assert payload["kind"] == "consent_channel_unavailable"

    async def test_refused_when_backend_uninitialized(self, monkeypatch):
        import agent as agent_mod
        payload_holder = {}

        async def run():
            import tools as tools_mod
            cm = MagicMock()
            cm.get = MagicMock(return_value=_FakeChannel())
            tools_mod.init_tools(
                channel_manager=cm, bus=MagicMock(),
                specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            )
            monkeypatch.setattr(agent_mod, "active_session_registry", None, raising=False)
            monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
            tok = _set_origin(agent_mod)
            try:
                res = await tools_mod.wipe_memory.handler({})
            finally:
                agent_mod.origin_var.reset(tok)
            payload_holder.update(_payload(res))

        await run()
        assert payload_holder["kind"] == "not_initialized"


class TestConsentFlow:
    async def test_posts_keyboard_and_awaits_user(self, monkeypatch, _fresh_broker):
        payload, channel = await _invoke(monkeypatch)
        assert payload["status"] == "awaiting_user"
        assert len(channel.posts) == 1
        assert _fresh_broker.pending(namespace="resident_ask", scope="authz:500")

    async def test_approve_runs_orchestrator_once_and_reports(
        self, monkeypatch, _fresh_broker,
    ):
        calls = []

        async def fake_wipe(**kwargs):
            calls.append(kwargs)
            return memory_wipe.WipeReport(
                spool_records_dropped=1, session_entries_dropped=2,
                bank_deleted=True,
            )

        monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", fake_wipe)
        payload, channel = await _invoke(monkeypatch)
        assert payload["status"] == "awaiting_user"
        await _tap(_fresh_broker, channel, idx=0)
        # Drain the detached wipe task.
        await memory_wipe.drain_wipe_task()
        await asyncio.sleep(0)
        assert len(calls) == 1
        # The keyboard message ends carrying the report.
        assert any("bank deleted=True" in e[2] for e in channel.edits)

    async def test_cancel_tap_deletes_nothing(self, monkeypatch, _fresh_broker):
        calls = []

        async def fake_wipe(**kwargs):
            calls.append(kwargs)
            return memory_wipe.WipeReport()

        monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", fake_wipe)
        payload, channel = await _invoke(monkeypatch)
        await _tap(_fresh_broker, channel, idx=1)    # "Cancel"
        await memory_wipe.drain_wipe_task()
        assert calls == []
        assert any("cancelled — nothing was deleted" in e[2] for e in channel.edits)

    async def test_non_operator_tap_cannot_claim(self, monkeypatch, _fresh_broker):
        """#469 operator-bound verdicts: a different actor's tap is forbidden
        at the broker, before any claim state is revealed."""
        payload, channel = await _invoke(monkeypatch)
        rid = _fresh_broker.pending(namespace="resident_ask", scope="authz:500")[0]
        claim = _fresh_broker.claim(
            namespace="resident_ask", scope="authz:500", request_id=rid,
            option_index=0, actor_id=666,
        )
        assert claim == "forbidden"

    async def test_broker_cancel_finishes_as_declined(
        self, monkeypatch, _fresh_broker,
    ):
        """A /new-style cancel_scope can only DENY — never execute."""
        calls = []

        async def fake_wipe(**kwargs):
            calls.append(kwargs)
            return memory_wipe.WipeReport()

        monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", fake_wipe)
        payload, channel = await _invoke(monkeypatch)
        n = _fresh_broker.cancel_scope(
            namespace="resident_ask", scope="authz:500", reason="new_session",
        )
        assert n == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await memory_wipe.drain_wipe_task()
        assert calls == []
        assert any("cancelled — nothing was deleted" in e[2] for e in channel.edits)

    async def test_second_wipe_refused_while_first_runs(
        self, monkeypatch, _fresh_broker,
    ):
        release = asyncio.Event()

        async def slow_wipe(**kwargs):
            await release.wait()
            return memory_wipe.WipeReport()

        monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", slow_wipe)
        payload, channel = await _invoke(monkeypatch)
        await _tap(_fresh_broker, channel, idx=0)
        # Second consent round while the first wipe still runs.
        payload2, channel2 = await _invoke(monkeypatch, channel=channel)
        await _tap(_fresh_broker, channel, idx=0)
        assert any("already running" in e[2] for e in channel.edits)
        release.set()
        await memory_wipe.drain_wipe_task()


class TestRegistration:
    def test_in_casa_tools_but_never_in_specialist_allowlist(self):
        import tools
        assert any(t.name == "wipe_memory" for t in tools.CASA_TOOLS)
        assert "wipe_memory" not in tools.SPECIALIST_CASA_TOOL_ALLOWLIST
        assert WIPE_TOOL not in tools.SPECIALIST_CASA_GRANTS
