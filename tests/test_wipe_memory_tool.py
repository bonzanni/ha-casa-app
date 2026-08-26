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

# #682: the literal the tool must answer with when its consent request was
# already settled while the keyboard was being delivered. Written out rather
# than imported from `tools` — a test that imports the string it asserts pins
# nothing, and an import of a not-yet-existing name would make the red case
# fail with an ImportError instead of for its intended reason.
SETTLED_WIPE_MESSAGE = (
    "the consent request settled before wipe_memory returned; "
    "no outcome is reported"
)


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


async def _invoke_raw(monkeypatch, *, channel=None, registry=None, sem=None,
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
    return result, channel


async def _invoke(monkeypatch, **kw):
    result, channel = await _invoke_raw(monkeypatch, **kw)
    return _payload(result), channel


def _wire(res):
    """The envelope's payload text with the request id normalised away."""
    payload = _payload(res)
    rid = payload.get("request_id")
    text = res["content"][0]["text"]
    return text.replace(rid, "<rid>") if isinstance(rid, str) else text


class _BlockingPostChannel(_FakeChannel):
    """`_FakeChannel` whose keyboard post blocks until released — the window
    between `register` and a confirmed post, which is where #682 lives."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.post_started = asyncio.Event()
        self.release_post = asyncio.Event()

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False):
        self.post_started.set()
        await self.release_post.wait()
        return await super().post_dm_keyboard(
            chat_id=chat_id, request_id=request_id, text=text,
            options=options, short_labels=short_labels)


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

    async def test_cancel_during_post_returns_neutral_settlement(
        self, monkeypatch, _fresh_broker,
    ):
        """RED pre-fix (#682, INV-TOOL-008): `/new` retires the consent request
        while its keyboard post is still in flight. `_run_setup` then records
        `message_id` on the request object the tool still holds, so the marker
        gate passes and the tool reports an outstanding request id for a
        request the broker has already retired and whose keyboard already reads
        "cancelled". Counts, not statuses."""
        calls = []

        async def fake_wipe(**kwargs):
            calls.append(kwargs)
            return memory_wipe.WipeReport()

        monkeypatch.setattr(memory_wipe, "wipe_long_term_memory", fake_wipe)
        channel = _BlockingPostChannel()
        task = asyncio.create_task(_invoke_raw(monkeypatch, channel=channel))
        await asyncio.wait_for(channel.post_started.wait(), 5.0)

        live = _fresh_broker.pending(namespace="resident_ask", scope="authz:500")
        assert len(live) == 1
        rid = live[0]
        assert _fresh_broker.cancel_scope(
            namespace="resident_ask", scope="authz:500",
            reason="new_session") == 1

        channel.release_post.set()
        raw, _ch = await asyncio.wait_for(task, 5.0)
        await _fresh_broker.drain_hooks()
        payload = _payload(raw)

        results = [payload]
        assert sum(p.get("status") == "settled" for p in results) == 1
        assert sum(p.get("status") == "awaiting_user" for p in results) == 0
        assert sum(p.get("request_id") == rid for p in results) == 1
        assert sum(p.get("message") == SETTLED_WIPE_MESSAGE
                   for p in results) == 1
        # The single read cannot tell a cancel from an Approve that won the
        # race, so the payload names no outcome — and in particular does not
        # claim that nothing was deleted.
        assert sum(bool({"outcome", "option_index", "reason", "kind"} & p.keys())
                   for p in results) == 0
        for claim in ("nothing was deleted", "wipe completed", "wipe failed",
                      "wipe aborted", "deleted"):
            assert sum(claim in p.get("message", "") for p in results) == 0
        # INV-TOOL-001: a `/new` is not a tool failure.
        assert sum("is_error" in envelope for envelope in [raw]) == 0

        assert len(_fresh_broker.pending(
            namespace="resident_ask", scope="authz:500")) == 0
        assert len(channel.posts) == 1
        assert len(channel.edits) == 1
        assert sum("cancelled — nothing was deleted" in e[2]
                   for e in channel.edits) == 1
        assert len(calls) == 0

    async def test_a_live_consent_request_returns_the_unchanged_payload(
        self, monkeypatch, _fresh_broker,
    ):
        """The control: nothing retires this request, so the happy path's wire
        payload stays byte-identical."""
        raw, _channel = await _invoke_raw(monkeypatch)
        expected = json.dumps({
            "status": "awaiting_user", "request_id": "<rid>",
            "message": (
                "consent keyboard posted to the operator; the wipe runs only "
                "on an explicit Approve tap and reports its result in that "
                "message"
            ),
        })
        assert sum(_wire(raw) == expected for _ in [0]) == 1
        assert sum("is_error" in envelope for envelope in [raw]) == 0
        assert len(_fresh_broker.pending(
            namespace="resident_ask", scope="authz:500")) == 1

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
