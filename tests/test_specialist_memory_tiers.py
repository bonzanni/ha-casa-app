"""Specialist memory read+write on the shared casa bank (tiered, plan 3).

_run_delegated_agent now inherits the PARENT context's channel clearance for
both read (delegated_recall → casa bank) and write (retain_delegated →
tier-classified, voice-gated).  The legacy MemoryProvider / add_turn / meta
session helpers are gone.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import tools

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fake semantic memory — mirrors _Sem from test_delegated_memory.py
# ---------------------------------------------------------------------------


def _hit(text):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1", document_id=None,
        chunk_id=None, source_fact_ids=None, metadata=None, context=None, score=None,
    )


class _FakeSem:
    def __init__(self, recall_ret: str = ""):
        self.recall_calls: list[dict] = []
        self.retain_calls: list[dict] = []
        self._recall_ret = recall_ret

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_calls.append({
            "bank": bank,
            "query": query,
            "tags": sorted(tags),
            "max_tokens": max_tokens,
            "budget": budget,
            "clearance": clearance,
        })
        return (_hit(self._recall_ret),) if self._recall_ret else ()

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


# ---------------------------------------------------------------------------
# Fake SDK client — mirrors _FakeSpecialistClient pattern
# ---------------------------------------------------------------------------


class _FakeSDKClient:
    """Minimal ClaudeSDKClient stand-in that captures the prompt and yields a reply.

    #699: this double used to yield an ``AssistantMessage`` and NOTHING else, so
    every test in this file ran a stream that ends without a ``ResultMessage`` —
    i.e. an ABORTED run (``DelegatedOutput.result_message_seen is False``) — while
    ``test_write_retain_telegram`` claimed to pin the SUCCESSFUL write path. The
    premise was never established. The default row now emits a real terminal
    ``ResultMessage(subtype="success")``; the abort rows say so explicitly, and
    the no-terminal-result row sets ``emit_result=False`` rather than
    synthesising a substitute verdict.
    """

    captured_prompt: str = ""
    response_text: str = "specialist reply"
    result_subtype: str | None = "success"
    result_is_error: bool = False
    emit_result: bool = True
    # None is what an older CLI reports and is the default everywhere else in
    # this file, so adding it leaves every existing row behaviourally unchanged.
    result_terminal_reason: str | None = None
    # Cluster S (#710): same rationale — None is the legacy/absent shape, so
    # every pre-existing row is unchanged by the knob's existence.
    result_stop_reason: str | None = None

    @classmethod
    def reset(cls, response: str = "specialist reply", *,
              result_subtype: str | None = "success",
              result_is_error: bool = False,
              emit_result: bool = True,
              result_terminal_reason: str | None = None,
              result_stop_reason: str | None = None) -> None:
        cls.captured_prompt = ""
        cls.response_text = response
        cls.result_subtype = result_subtype
        cls.result_is_error = result_is_error
        cls.emit_result = emit_result
        cls.result_terminal_reason = result_terminal_reason
        cls.result_stop_reason = result_stop_reason

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, text: str) -> None:
        type(self).captured_prompt = text

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        try:
            block = TextBlock(text=type(self).response_text)
        except TypeError:
            block = TextBlock(type(self).response_text)  # type: ignore[call-arg]
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst
        if not type(self).emit_result:
            return
        # SDK shape drift: mirror the kwargs / __new__ fallback already used by
        # `_FakeSpecialistClient` in tests/test_delegate_to_agent.py.
        try:
            result = ResultMessage(session_id="exec-sid")
        except TypeError:
            result = ResultMessage.__new__(ResultMessage)
            result.session_id = "exec-sid"  # type: ignore[attr-defined]
        object.__setattr__(result, "structured_output", None)
        object.__setattr__(result, "subtype", type(self).result_subtype)
        object.__setattr__(result, "is_error", type(self).result_is_error)
        object.__setattr__(result, "num_turns", 2)
        object.__setattr__(
            result, "terminal_reason", type(self).result_terminal_reason)
        object.__setattr__(
            result, "stop_reason", type(self).result_stop_reason)
        yield result


# ---------------------------------------------------------------------------
# #699 literals — the exact retain items this file's synthetic origin produces.
# Verified by evaluating memory_provenance.build_retain_items directly at the
# base commit: this fixture carries no caller speaker_provenance and the cfg has
# no bound identity, so both parties fall back to the unattributed "system"
# identity and both ids live in the m-a- agent id space.
# ---------------------------------------------------------------------------

_SYSTEM_METADATA = (
    '{"binding_digest":null,"display_name":null,"persona_id":null,'
    '"persona_version":null,"role_id":null,"speaker_kind":"system",'
    '"user_id":null,"user_peer":null}'
)
_SYSTEM_SOURCE_TAG = (
    "casa-source-v1.eyJiaW5kaW5nX2RpZ2VzdCI6bnVsbCwiZGlzcGxheV9uYW1lIjpudWxsLCJwZX"
    "Jzb25hX2lkIjpudWxsLCJwZXJzb25hX3ZlcnNpb24iOm51bGwsInJvbGVfaWQiOm51bGwsInNwZWFr"
    "ZXJfa2luZCI6InN5c3RlbSIsInVzZXJfaWQiOm51bGwsInVzZXJfcGVlciI6bnVsbH0"
)

# task_text "Q1 cashflow?" → the classifier stub below returns "private"
_CALLER_ITEM = {
    "content": "Q1 cashflow?",
    "tags": ["private", _SYSTEM_SOURCE_TAG],
    "metadata": {"casa_source_v1": _SYSTEM_METADATA},
    "document_id": "m-a-ea4eb34f5715b26d6b5867ea",
}
# the specialist's accumulated text on an ABORTED run — never retainable
_PARTIAL_ANSWER_ITEM = {
    "content": "partial answer",
    "tags": ["friends", _SYSTEM_SOURCE_TAG],
    "metadata": {"casa_source_v1": _SYSTEM_METADATA},
    "document_id": "m-a-2824e69d9906c4a76808e9fd",
}
# a COMPLETED specialist answer — retainable
_COMPLETE_ANSWER_ITEM = {
    "content": "Q1 is on track",
    "tags": ["friends", _SYSTEM_SOURCE_TAG],
    "metadata": {"casa_source_v1": _SYSTEM_METADATA},
    "document_id": "m-a-6302e7710cd408202a37846a",
}


async def _tier_stub(text):
    """Deterministic classifier: the caller's question is private, answers are not."""
    return "private" if "cashflow" in text.lower() else "friends"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _specialist_cfg(role: str = "finance", token_budget: int = 4000):
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
    )
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt=f"You are {role}",
        character=CharacterConfig(name=role.capitalize()),
        enabled=True,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=token_budget),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


def _set_origin(monkeypatch, *, channel: str = "telegram", role: str = "assistant",
                chat_id: str = "abc", cid: str = "cid42", scope: str = "personal") -> None:
    import agent as agent_mod
    agent_mod.origin_var.set({
        "role": role,
        "channel": channel,
        "chat_id": chat_id,
        "cid": cid,
        "scope": scope,
        "delegation_depth": 0,
    })


async def _drain_bg() -> None:
    bg = getattr(tools, "_specialist_bg_tasks", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 1: Read at inherited clearance (telegram → private + broader)
# ---------------------------------------------------------------------------


async def test_read_at_telegram_clearance(monkeypatch):
    """parent channel=telegram → recall tags include private,family,friends,public."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    task_text = "how is Q1 cashflow?"
    digest = "## Summary\nQ1 spend: €1200\n"
    fake_sem = _FakeSem(recall_ret=digest)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(cfg, task_text=task_text, context_text="")

    assert len(fake_sem.recall_calls) == 1
    c = fake_sem.recall_calls[0]
    assert c["bank"] == "casa"
    assert c["query"] == task_text
    assert c["tags"] == ["family", "friends", "private", "public"]

    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance">' in prompt
    assert "Q1 spend" in prompt
    assert "</memory_context>" in prompt
    # Ordering: delegation_context → memory_context → Task:
    assert prompt.index("<delegation_context>") < prompt.index('<memory_context') < prompt.index("Task:")


# ---------------------------------------------------------------------------
# Test 2: token_budget=0 → no recall, no retain
# ---------------------------------------------------------------------------


async def test_token_budget_zero_no_sem_calls(monkeypatch):
    """token_budget=0 preserves the stateless path — no recall and no retain."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=0)
    fake_sem = _FakeSem(recall_ret="something")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    await _drain_bg()
    # #699: a genuinely SUCCESSFUL run — the zero-budget gate, not an abort, is
    # what suppresses both calls here.
    assert output == tools.DelegatedOutput(
        text="ok", structured_output=None,
        run_subtype="success", result_message_seen=True,
    )
    assert fake_sem.recall_calls == []
    assert fake_sem.retain_calls == []


# ---------------------------------------------------------------------------
# Test 3: Write via retain (telegram, non-empty reply)
# ---------------------------------------------------------------------------


async def test_write_retain_telegram(monkeypatch):
    """Telegram parent → retain fires; items have correct turns + document_ids."""
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(response="Q1 is on track")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    # #699: this test's premise — that the run SUCCEEDED — is now established
    # rather than assumed. Before the double emitted a terminal ResultMessage
    # this was an aborted run asserting the successful path's behaviour.
    assert out == tools.DelegatedOutput(
        text="Q1 is on track", structured_output=None,
        run_subtype="success", result_message_seen=True,
    )
    assert out.run_aborted is False

    await _drain_bg()

    # Task 10: content-addressed ids (the retired doc_prefix:idx scheme is gone).
    # Asserted as full literal items — the previous prefix/tag-count proxies are
    # subsumed by equality, and a proxy is what lets a wrong item pass.
    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == 2
    assert fake_sem.retain_calls == [{
        "bank": "casa",
        "items": [_CALLER_ITEM, _COMPLETE_ANSWER_ITEM],
    }]


# ---------------------------------------------------------------------------
# Test 4: Voice writes nothing (recall still fires)
# ---------------------------------------------------------------------------


async def test_voice_writes_nothing(monkeypatch):
    """Voice parent → write-trust gate fires; zero retain calls.
    Recall still fires (voice read-clearance = friends+public)."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="some prior fact")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="voice")
    _FakeSDKClient.reset(response="voice answer")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="hello?", context_text="")

    await _drain_bg()

    # #699: the run SUCCEEDED — write-trust, not the terminal verdict, is what
    # keeps voice out of the bank.
    assert out == tools.DelegatedOutput(
        text="voice answer", structured_output=None,
        run_subtype="success", result_message_seen=True,
    )
    assert fake_sem.retain_calls == []
    # Recall did fire, with voice clearance tags
    assert len(fake_sem.recall_calls) == 1
    assert fake_sem.recall_calls[0]["tags"] == ["friends", "public"]


# ---------------------------------------------------------------------------
# Test 5: Empty reply → no retain
# ---------------------------------------------------------------------------


async def test_empty_reply_retains_caller_turn_only(monkeypatch):
    """An empty SDK reply no longer suppresses the whole retain (#708): the
    caller's non-blank task turn — which nothing else writes — is still
    submitted, alone; the empty answer contributes no item."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="")  # empty SDK reply

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="")

    await _drain_bg()
    assert out == tools.DelegatedOutput(
        text="", structured_output=None,
        run_subtype="success", result_message_seen=True,
    )
    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == 1
    assert fake_sem.retain_calls[0]["items"][0]["content"] == "hi"


# ---------------------------------------------------------------------------
# Test 5b: Recall unavailable → explicit status note, never a fake digest
# ---------------------------------------------------------------------------


async def test_recall_unavailable_injects_status_note(monkeypatch):
    """When memory could not be checked, the specialist is told so explicitly
    — a silent cold turn would let it claim Casa lacks information."""
    import agent as agent_mod
    from semantic_memory import RecallUnavailable

    class _DownSem(_FakeSem):
        async def recall_items(self, *a, **k):
            raise RecallUnavailable("http_504")

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", _DownSem(), raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    assert output.text == "ok"                      # turn still completes
    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance" status="unavailable">' in prompt
    assert "could not be checked" in prompt
    # No fabricated digest content beyond the status note.


# ---------------------------------------------------------------------------
# Test 6: Legacy helpers are gone
# ---------------------------------------------------------------------------


def test_legacy_helpers_removed():
    """The bespoke add_turn / meta-write helpers must have been deleted."""
    assert not hasattr(tools, "_specialist_add_turn_bg"), (
        "_specialist_add_turn_bg still exists — should have been removed"
    )
    assert not hasattr(tools, "_specialist_meta_write_bg"), (
        "_specialist_meta_write_bg still exists — should have been removed"
    )


# ---------------------------------------------------------------------------
# Test 7: Boot-degraded (active_semantic_memory is None) — no crash
# ---------------------------------------------------------------------------


async def test_sem_none_no_crash(monkeypatch):
    """active_semantic_memory unset (boot-degraded) → specialist runs, gets the
    unavailability note (memory can't be checked ≠ no memories), no crash."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    assert output.text == "ok"
    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance" status="unavailable">' in prompt
    assert "could not be checked" in prompt


# ---------------------------------------------------------------------------
# Test 8 (#205): the specialist read is labelled specialist_archive
# ---------------------------------------------------------------------------


async def test_specialist_recall_records_specialist_archive_path(monkeypatch):
    """The specialist memory read must name its own recall path.

    Before #205 this call site passed no ``path=``, so it fell to
    ``delegated_recall``'s generic ``"delegated"`` default: its telemetry was
    indistinguishable from any other delegated recall, and it shared that
    path's circuit breaker. Guard both halves — the recorded label AND the
    breaker registry key, since ``recall_health._breaker_for`` caches one
    breaker per path.
    """
    import agent as agent_mod
    from recall_health import (
        _PATH_BREAKERS, default_telemetry, reset_recall_breakers,
    )

    reset_recall_breakers()
    before = len(default_telemetry().snapshot())

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="## Summary\nQ1 spend: EUR 1200\n")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="how is Q1 cashflow?", context_text="")

    new_events = default_telemetry().snapshot()[before:]
    assert [e.path for e in new_events] == ["specialist_archive"], (
        "the specialist memory read must record path=specialist_archive, not "
        "the generic 'delegated' default"
    )
    assert "specialist_archive" in _PATH_BREAKERS
    assert "delegated" not in _PATH_BREAKERS, (
        "the specialist read must no longer open/consume the generic "
        "'delegated' breaker"
    )
    reset_recall_breakers()


# ---------------------------------------------------------------------------
# #336 — a delegated specialist reads at the DELEGATING TURN's clearance
# ---------------------------------------------------------------------------


async def test_non_operator_delegation_reads_public_only(monkeypatch):
    """Sol r3/r4: a non-operator Telegram sender must not reach private memory
    by having a specialist fetch it. End-to-end through _run_delegated_agent,
    asserting what the recall actually received.

    Red case demonstrated: dropping origin_route/origin_clearance from the
    specialist's delegated_recall call restores channel-keyed private
    clearance and this test fails."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="## Summary\nnothing sensitive\n")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "abc",
        "cid": "cid42", "scope": "personal", "delegation_depth": 0,
        # The stamped markers of a stranger's turn.
        "_origin_route": "telegram", "_origin_clearance": "public",
    })
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="what is the alarm code?", context_text="")

    c = fake_sem.recall_calls[0]
    assert c["clearance"] == "public"
    assert c["tags"] == ["public"]


async def test_operator_delegation_still_reads_every_tier(monkeypatch):
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="## Summary\nQ1 spend: 1200\n")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "abc",
        "cid": "cid42", "scope": "personal", "delegation_depth": 0,
        "_origin_route": "telegram", "_origin_clearance": "private",
    })
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="how is Q1 cashflow?", context_text="")

    c = fake_sem.recall_calls[0]
    assert c["clearance"] == "private"
    assert c["tags"] == ["family", "friends", "private", "public"]


# ---------------------------------------------------------------------------
# #699 / INV-MEM-016: an aborted delegated run's partial answer is never banked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("result_subtype,emit_result,result_is_error", [
    pytest.param("error_max_turns", True, True, id="error-max-turns"),
    pytest.param("error_max_budget_usd", True, True, id="error-max-budget-usd"),
    pytest.param("error_max_structured_output_retries", True, True,
                 id="error-max-structured-output-retries"),
    pytest.param("error_during_execution", True, True, id="error-during-execution"),
    pytest.param("error_something_new", True, True, id="unknown-error-subtype"),
    # The abort verdict is the SUBTYPE, never the `is_error` flag beside it.
    # Without this row an implementation reading `is_error` instead of
    # `run_aborted` passes every other row, because they all carry is_error=True
    # (red-case acceptance round 1).
    pytest.param("error_something_new", True, False,
                 id="unknown-error-subtype-is-error-false"),
    pytest.param(None, False, False, id="no-result-message"),
])
async def test_pin_inv_mem_016_aborted_run_retains_only_caller_task(
    monkeypatch, result_subtype, emit_result, result_is_error,
):
    """INV-MEM-016: when the CLI ends the run without completing the turn, the
    accumulated partial answer is NEVER retained to the shared casa bank — while
    the caller's own task turn, which nothing else writes, still is.

    Red at 10604c19 on `len(items) == 1`, observed 2: the second item is exactly
    `_PARTIAL_ANSWER_ITEM`, banked as an ordinary provenance-attributed document
    indistinguishable from a completed exchange.
    """
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer",
        result_subtype=result_subtype,
        result_is_error=result_is_error,
        emit_result=emit_result,
    )

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    # The whole verdict, not just the derived flag. `run_is_error` is now
    # RECORDED on the output (cluster S) — for an aborted run it is evidence
    # only: the abort taxonomy wins (`caller_error_kind` is None).
    assert out == tools.DelegatedOutput(
        text="partial answer",
        structured_output=None,
        run_subtype=result_subtype,
        result_message_seen=emit_result,
        run_is_error=result_is_error,
    )
    assert out.run_aborted is True

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == 1
    assert fake_sem.retain_calls == [{"bank": "casa", "items": [_CALLER_ITEM]}]


async def test_pin_inv_mem_016_seen_result_without_subtype_retains_both_turns(
    monkeypatch,
):
    """The deliberate asymmetry of `DelegatedOutput.run_aborted`: a ResultMessage
    that WAS seen but carries no subtype is a completed run (legacy/test
    construction), so both turns are still retained.

    This is the control that a `subtype == "success"` re-expression of the
    predicate would break.
    """
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(response="partial answer", result_subtype=None)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out == tools.DelegatedOutput(
        text="partial answer", structured_output=None,
        run_subtype=None, result_message_seen=True,
    )
    assert out.run_aborted is False

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == 2
    assert fake_sem.retain_calls == [{
        "bank": "casa",
        "items": [_CALLER_ITEM, _PARTIAL_ANSWER_ITEM],
    }]


@pytest.mark.parametrize("result_subtype,emit_result", [
    pytest.param("success", True, id="completed"),
    pytest.param("error_max_turns", True, id="aborted"),
    pytest.param(None, False, id="aborted-no-result-message"),
])
async def test_empty_answer_submits_caller_turn_whether_or_not_run_aborted(
    monkeypatch, result_subtype, emit_result,
):
    """#708 (cluster S red case): an empty specialist answer must not discard
    the caller's task turn — its ONLY writer is this retain.

    This REVISES (never deletes) the pin that previously froze the outer
    `and text` gate's symmetric zero-retain behaviour: that gate keyed the
    whole retain on answer text, so a run with an empty answer — completed and
    aborted alike — silently lost the caller's true utterance. Admission is
    per turn now: the non-blank caller turn is submitted in all three rows
    while the (empty) answer contributes nothing. Fails pre-fix with zero
    retain calls at the outer gate (tools.py:2974).
    """
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="", result_subtype=result_subtype, emit_result=emit_result)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out == tools.DelegatedOutput(
        text="", structured_output=None,
        run_subtype=result_subtype, result_message_seen=emit_result,
    )
    assert out.run_aborted is (result_subtype != "success")

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == 1
    assert fake_sem.retain_calls == [{"bank": "casa", "items": [_CALLER_ITEM]}]


async def test_aborted_voice_run_writes_nothing_and_claims_nothing(
    monkeypatch, caplog,
):
    """An aborted run on a NON-write-trusted origin writes nothing at all — and
    the withhold log must not claim otherwise.

    The log line fires at the assembly site, before the detached best-effort
    writer runs, so it can only honestly report what that site DECIDES: that the
    partial answer is excluded. It previously added "the caller's task turn still
    is" retained, which is false here — `retain_delegated` returns on a voice
    origin before writing anything — and equally false under a wipe landing first
    or a backend failure, both of which this site cannot see.
    """
    import logging

    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="voice", cid="cid42")
    _FakeSDKClient.reset(response="partial answer", result_subtype="error_max_turns")

    with caplog.at_level(logging.WARNING, logger="tools"):
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            out = await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_aborted is True

    await _drain_bg()

    # Nothing reached the bank at all — not the answer, not the caller's turn.
    assert fake_sem.retain_calls == []

    withheld = [r.getMessage() for r in caplog.records
                if "did not complete" in r.getMessage()]
    assert len(withheld) == 1
    # The whole sentence, not a word of it: assert what the line SAYS.
    #
    # The role token is rendered by `_known_role`, which reports "<other>" for a
    # role absent from the module-level agent map — and whether "finance" is in
    # that map depends on what else has run in this worker, so hardcoding either
    # rendering makes this test pass or fail on its neighbours. It did: this
    # assertion failed intermittently under the parallel suite while passing
    # every targeted run. The rendering is asked for here rather than assumed,
    # which removes the coupling without weakening the assertion to a substring.
    assert withheld[0] == (
        f"delegated agent {tools._known_role('finance')} run did not complete "
        "(kind=specialist_turn_limit) — its partial answer is excluded from "
        "the memory retain"
    )


@pytest.mark.parametrize("is_error,terminal_reason", [
    # The SDK's own documented shape for a failing API call: `api_error_status`
    # is defined as set "when is_error is True and subtype is 'success'".
    pytest.param(True, None, id="api-error-under-a-success-subtype"),
    pytest.param(True, "completed", id="api-error-with-a-completed-reason"),
    # The CLI reporting the turn was cancelled mid-stream.
    pytest.param(False, "aborted_streaming", id="cancelled-streaming"),
    pytest.param(False, "aborted_tools", id="cancelled-tools"),
    # `terminal_reason` is an OPEN namespace — the SDK types it `str | None` and
    # passes the CLI's value through verbatim. These rows exist because a
    # deny-list of the two cancellation reasons above read every OTHER reason as
    # a finished turn; the test is against an allow-list of completed ones, so a
    # reason nobody listed fails CLOSED.
    pytest.param(False, "max_turns", id="unlisted-reason-max-turns"),
    pytest.param(False, "model_error", id="unlisted-reason-model-error"),
    pytest.param(False, "prompt_too_long", id="unlisted-reason-prompt-too-long"),
    pytest.param(False, "a_reason_this_release_has_never_seen",
                 id="unlisted-reason-from-the-future"),
])
async def test_a_success_subtype_is_not_enough_to_call_the_turn_complete(
    monkeypatch, is_error, terminal_reason,
):
    """INV-MEM-016 covers every way the CLI says the turn did not finish.

    `subtype` alone is not the verdict. A terminal result carrying
    `is_error=True` under `subtype="success"` is an API failure, and a
    non-completed `terminal_reason` is a turn the loop ended for another
    cause. Since cluster S (#709) these END the run as a typed caller fault
    RAISED before retention — the #568 refusal shape — so the half-finished
    exchange banks nothing at all: the raise reaches the delegation
    consumers' exception arms, which record the failure durably; there is no
    completed exchange for the bank. The pre-#709 rows that expected a
    caller-turn-only retain moved here as zero-retain rows on the record
    (converged design, rejected-disagreement #1/#4)."""
    import agent as agent_mod
    import delegated_memory
    from error_kinds import ApiErrorTurn

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_is_error=is_error, result_terminal_reason=terminal_reason)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        with pytest.raises(ApiErrorTurn):
            await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")

    await _drain_bg()

    assert fake_sem.retain_calls == []


@pytest.mark.parametrize("terminal_reason", [
    pytest.param("completed", id="control-completed"),
    pytest.param(None, id="control-no-terminal-reason"),
])
async def test_a_genuinely_completed_run_still_retains_both_turns(
    monkeypatch, terminal_reason,
):
    """Controls for the caller-fault raise: a genuinely completed run, with
    and without a terminal reason (older CLIs report none), still retains
    both turns and raises nothing."""
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_is_error=False, result_terminal_reason=terminal_reason)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_subtype == "success"
    assert out.run_aborted is False

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert fake_sem.retain_calls == [{
        "bank": "casa", "items": [_CALLER_ITEM, _PARTIAL_ANSWER_ITEM],
    }]


def test_capture_layer_predicates_are_not_collapsed():
    """Capture-layer control (Sol, red-case round): `run_aborted` stays a
    SUBTYPE verdict — `is_error` and terminal/stop evidence widen the
    caller-fault and memory predicates, never `run_aborted` itself, whose
    four non-memory consumers pin the abort taxonomy."""
    out = tools.DelegatedOutput(
        text="x", run_subtype="success", run_is_error=True,
        run_terminal_reason="aborted_streaming", run_stop_reason="max_tokens",
    )
    assert out.run_aborted is False
    assert out.answer_incomplete is True
    assert out.caller_error_kind is not None


# ---------------------------------------------------------------------------
# Cluster S red case (#710): an output-token-truncated answer is not complete
# ---------------------------------------------------------------------------


async def test_red_incomplete_stop_reason_withholds_only_the_answer(
    monkeypatch, caplog,
):
    """#710 (cluster S red case): `stop_reason="max_tokens"` on an otherwise
    completed result means the model did NOT finish the answer — the truncated
    text must be withheld from the bank (audibly) while the caller's task turn
    is still submitted. The caller-facing verdict is untouched:
    `run_aborted` stays False. Fails pre-fix with 2 submitted items and 0
    warnings because the retain gate never reads `stop_reason`
    (tools.py:3017-3034)."""
    import logging

    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_terminal_reason="completed", result_stop_reason="max_tokens")

    with caplog.at_level(logging.WARNING, logger="tools"):
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            out = await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")

    # The caller-facing verdict is NOT a function of stop_reason.
    assert out.run_aborted is False

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == 1
    assert fake_sem.retain_calls == [{"bank": "casa", "items": [_CALLER_ITEM]}]

    withheld = [r.getMessage() for r in caplog.records
                if "excluded from the memory retain" in r.getMessage()]
    assert len(withheld) == 1


@pytest.mark.parametrize("stop_reason,expected_items,expect_warning", [
    pytest.param("tool_use", 1, True, id="tool-use-is-an-unfinished-answer"),
    pytest.param("a_stop_reason_nobody_listed", 1, True, id="unknown-stop-reason"),
    pytest.param("end_turn", 2, False, id="control-end-turn"),
    pytest.param("stop_sequence", 2, False, id="control-stop-sequence"),
    pytest.param(None, 2, False, id="control-legacy-absent"),
])
async def test_stop_reason_matrix(
    monkeypatch, caplog, stop_reason, expected_items, expect_warning,
):
    """#710: the answer is admitted only under a completed stop reason —
    ALLOW-list direction, `None` legacy-completed; `tool_use` is excluded
    because on the memory-writing paths (`output_format=None`) a terminal
    tool call is an explicitly unfinished answer (seam round 1)."""
    import logging

    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_stop_reason=stop_reason)

    with caplog.at_level(logging.WARNING, logger="tools"):
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            out = await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_aborted is False

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert len(fake_sem.retain_calls[0]["items"]) == expected_items
    expected = [_CALLER_ITEM] if expected_items == 1 else [
        _CALLER_ITEM, _PARTIAL_ANSWER_ITEM]
    assert fake_sem.retain_calls == [{"bank": "casa", "items": expected}]

    withheld = [r.getMessage() for r in caplog.records
                if "excluded from the memory retain" in r.getMessage()]
    assert len(withheld) == (1 if expect_warning else 0)


async def test_malformed_stop_reason_withholds_the_answer(monkeypatch):
    """#710/seam round 1: a malformed (non-string) stop reason is the
    fail-closed sentinel, never legacy None — exactly one caller item."""
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_stop_reason=789)  # type: ignore[arg-type]

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_aborted is False

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    assert fake_sem.retain_calls == [{"bank": "casa", "items": [_CALLER_ITEM]}]


@pytest.mark.parametrize("response,stop_reason", [
    pytest.param("", None, id="blank-task-blank-answer"),
    pytest.param("partial answer", "max_tokens", id="blank-task-withheld-answer"),
])
async def test_whitespace_only_task_submits_nothing(
    monkeypatch, response, stop_reason,
):
    """#708: a whitespace-only task is BLANK — with no admissible answer the
    retain is never invoked at all (`retain_delegated` never sees an empty
    turns list). Kills the truthiness-for-strip mutant (seam round 2)."""
    import agent as agent_mod
    import delegated_memory

    monkeypatch.setattr(delegated_memory, "classify_tier", _tier_stub)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response=response, result_subtype="success",
        result_stop_reason=stop_reason)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="   ", context_text="")

    await _drain_bg()

    assert fake_sem.retain_calls == []
