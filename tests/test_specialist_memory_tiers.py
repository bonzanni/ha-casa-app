"""Specialist memory read and write, at the PARENT context's clearance.

_run_delegated_agent reads the specialist's view of the ledger that clearance
reads, and captures the exchange into the memory store (write-trusted
channels only; an unfinished answer never). The legacy MemoryProvider /
add_turn / meta session helpers are gone.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import tools

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fake SDK client — mirrors _FakeSpecialistClient pattern
# ---------------------------------------------------------------------------


class _FakeSDKClient:
    """Minimal ClaudeSDKClient stand-in that captures the prompt and yields a reply.

    #699: this double used to yield an ``AssistantMessage`` and NOTHING else, so
    every test in this file ran a stream that ends without a ``ResultMessage`` —
    i.e. an ABORTED run (``DelegatedOutput.result_message_seen is False``) — while
    the write test claimed to pin the SUCCESSFUL write path. The
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


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """A real store wired into the live capture seams.

    Local rather than imported: ``tests/test_memory_capture.py`` imports this
    module's client double, so importing its fixture back would cycle."""
    from memory import capture
    from memory.store import MemoryStore
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
    monkeypatch.setattr(capture, "_noticed", set())
    monkeypatch.setattr(capture, "_background", set())
    monkeypatch.setattr(capture, "_drain", [None])
    return store


def _captured(store) -> list[str]:
    """The texts captured into ``events``, in the order they occurred. A
    capture that met a busy store (a loaded machine holds the write lock past
    capture's 50 ms bound) is durable in the spool instead; it is drained
    first, so the question is what was captured, not where it waited. A
    drained item is stored after the ones that followed it, so rows are
    ordered by when they occurred, not by when they were stored."""
    import sqlite3
    from memory import capture
    capture.drain_waiting(store)
    conn = sqlite3.connect(store.path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT text FROM events ORDER BY occurred_at, seq")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: Read at inherited clearance (telegram → private + broader)
# ---------------------------------------------------------------------------


async def test_read_at_telegram_clearance(monkeypatch, tmp_path):
    """Parent channel=telegram (private) → the specialist's prompt carries
    its view of the private ledger, between the delegation context and the
    task; nothing is recalled."""
    from memory_serving_support import fact, ledger_store, wire
    wire(monkeypatch, ledger_store(tmp_path, [fact("Q1 spend", "€1200")]))
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="how is Q1 cashflow?", context_text="")

    prompt = _FakeSDKClient.captured_prompt
    assert "<memory>" in prompt and "Q1 spend: €1200" in prompt
    assert "</memory>" in prompt
    # Ordering: delegation_context → memory → Task:
    assert prompt.index("<delegation_context>") < prompt.index("<memory>") \
        < prompt.index("Task:")


# ---------------------------------------------------------------------------
# Test 2: token_budget=0 → no view, no capture
# ---------------------------------------------------------------------------


async def test_token_budget_zero_captures_nothing(monkeypatch, mem):
    """token_budget=0 preserves the stateless path — nothing captured."""
    cfg = _specialist_cfg(role="finance", token_budget=0)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    # #699: a genuinely SUCCESSFUL run — the zero-budget gate, not an abort, is
    # what suppresses both calls here.
    assert output == tools.DelegatedOutput(
        text="ok", structured_output=None,
        run_subtype="success", result_message_seen=True, reply_messages=("ok",),
    )
    assert _captured(mem) == []


# ---------------------------------------------------------------------------
# Test 3: Write via capture (telegram, non-empty reply)
# ---------------------------------------------------------------------------


async def test_write_capture_telegram(monkeypatch, mem):
    """Telegram parent → the request and the answer are captured, in order."""


    cfg = _specialist_cfg(role="finance", token_budget=4000)
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
        run_subtype="success", result_message_seen=True, reply_messages=("Q1 is on track",),
    )
    assert out.run_aborted is False


    # Task 10: content-addressed ids (the retired doc_prefix:idx scheme is gone).
    # Asserted as full literal items — the previous prefix/tag-count proxies are
    # subsumed by equality, and a proxy is what lets a wrong item pass.
    assert _captured(mem) == ["Q1 cashflow?", "Q1 is on track"]


# ---------------------------------------------------------------------------
# Test 4: Voice writes nothing (recall still fires)
# ---------------------------------------------------------------------------


async def test_voice_writes_nothing(monkeypatch, mem):
    """Voice parent → write-trust gate fires; nothing captured. Voice reads
    at friends+public, where no ledger is maintained."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="voice")
    _FakeSDKClient.reset(response="voice answer")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="hello?", context_text="")


    # #699: the run SUCCEEDED — write-trust, not the terminal verdict, is what
    # keeps voice out of memory.
    assert out == tools.DelegatedOutput(
        text="voice answer", structured_output=None,
        run_subtype="success", result_message_seen=True, reply_messages=("voice answer",),
    )
    assert _captured(mem) == []
    # Voice reads at ``friends``, where no ledger is maintained: no memory.
    assert "<memory" not in _FakeSDKClient.captured_prompt


# ---------------------------------------------------------------------------
# Test 5: Empty reply → the request alone
# ---------------------------------------------------------------------------


async def test_empty_reply_captures_caller_turn_only(monkeypatch, mem):
    """An empty SDK reply does not suppress the request (#708): the caller's
    non-blank task turn is captured, alone; the empty answer contributes
    nothing."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="")  # empty SDK reply

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="")

    assert out == tools.DelegatedOutput(
        text="", structured_output=None,
        run_subtype="success", result_message_seen=True,
    )
    assert _captured(mem) == ["hi"]


# ---------------------------------------------------------------------------
# Test 5b: Memory unreadable → explicit status note, never a fake digest
# ---------------------------------------------------------------------------


async def test_recall_unavailable_injects_status_note(monkeypatch, tmp_path):
    """When memory could not be read, the specialist is told so explicitly
    — a silent cold turn would let it claim Casa lacks information."""
    from memory_serving_support import ledger_store, wire
    store = ledger_store(tmp_path)
    store._unavailable_reason = "a simulated outage"
    wire(monkeypatch, store)
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    assert output.text == "ok"                      # turn still completes
    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance" status="unavailable">' in prompt
    assert "could not be checked" in prompt
    assert "<memory>" not in prompt


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
# Test 7: Boot-degraded (no memory store wired) — no crash
# ---------------------------------------------------------------------------


async def test_sem_none_no_crash(monkeypatch):
    """No memory store wired (boot-degraded) → the specialist runs, gets the
    unavailability note (memory can't be read ≠ no memories), no crash."""
    from memory_serving_support import wire
    wire(monkeypatch, None)
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
# #336 — a delegated specialist reads at the DELEGATING TURN's clearance
# ---------------------------------------------------------------------------


async def test_non_operator_delegation_reads_public_only(monkeypatch, tmp_path):
    """Sol r3/r4: a non-operator Telegram sender must not reach private memory
    by having a specialist fetch it. End-to-end through _run_delegated_agent,
    asserting what the specialist's prompt actually carries: a ``public``
    reader is served no view of the private ledger.

    Red case: reading the clearance from the channel (``telegram`` →
    ``private``) instead of the delegating turn's stamped markers serves the
    private ledger and this test fails."""
    import agent as agent_mod
    from memory_serving_support import fact, ledger_store, wire
    wire(monkeypatch, ledger_store(tmp_path, [fact("alarm", "code 4417")]))
    cfg = _specialist_cfg(role="finance", token_budget=4000)
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

    prompt = _FakeSDKClient.captured_prompt
    assert "4417" not in prompt and "<memory" not in prompt


async def test_operator_delegation_still_reads_every_tier(monkeypatch, tmp_path):
    import agent as agent_mod
    from memory_serving_support import fact, ledger_store, wire
    wire(monkeypatch, ledger_store(tmp_path, [fact("Q1 spend", "1200")]))
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "abc",
        "cid": "cid42", "scope": "personal", "delegation_depth": 0,
        "_origin_route": "telegram", "_origin_clearance": "private",
    })
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="how is Q1 cashflow?", context_text="")

    assert "Q1 spend: 1200" in _FakeSDKClient.captured_prompt


# ---------------------------------------------------------------------------
# #699 / INV-MEM-016: an aborted delegated run's partial answer is never captured
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
async def test_pin_inv_mem_016_aborted_run_captures_only_caller_task(
    monkeypatch, result_subtype, emit_result, result_is_error, mem,
):
    """INV-MEM-016: when the CLI ends the run without completing the turn, the
    accumulated partial answer is NEVER captured — while the caller's own
    task turn still is."""


    cfg = _specialist_cfg(role="finance", token_budget=4000)
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
        reply_messages=("partial answer",),
    )
    assert out.run_aborted is True


    assert _captured(mem) == ["Q1 cashflow?"]


async def test_pin_inv_mem_016_null_subtype_on_a_parsed_result_is_an_abort(
    monkeypatch, mem,
):
    """OVERTURNED on the record (Sol review r2, S1): `subtype` is a REQUIRED
    field on a parsed ResultMessage (`data["subtype"]`), so a null there is
    the CLI writing the wrong value into a required field — malformed
    evidence, an abort — not the older-CLI legacy case (omission raises in
    the parser and no ResultMessage exists at all). This pin previously
    asserted the opposite (both turns kept); the capture boundary now
    fails closed, and only a DIRECT DelegatedOutput construction keeps the
    no-subtype-is-completed default (asserted below).
    """


    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(response="partial answer", result_subtype=None)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_subtype == tools._MALFORMED_EVIDENCE
    assert out.run_aborted is True


    assert _captured(mem) == ["Q1 cashflow?"]

    # The object-level asymmetry survives for direct constructions: absent
    # subtype on a seen result stays completed there, which is what every
    # legacy/test construction relies on.
    assert tools.DelegatedOutput(text="x").run_aborted is False


@pytest.mark.parametrize("result_subtype,emit_result", [
    pytest.param("success", True, id="completed"),
    pytest.param("error_max_turns", True, id="aborted"),
    pytest.param(None, False, id="aborted-no-result-message"),
])
async def test_empty_answer_submits_caller_turn_whether_or_not_run_aborted(
    monkeypatch, result_subtype, emit_result, mem,
):
    """#708 (cluster S red case): an empty specialist answer must not discard
    the caller's task turn. Admission is per turn: the non-blank caller turn
    is captured in all three rows while the (empty) answer contributes
    nothing."""


    cfg = _specialist_cfg(role="finance", token_budget=4000)
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


    assert _captured(mem) == ["Q1 cashflow?"]


async def test_aborted_voice_run_writes_nothing_and_claims_nothing(
    monkeypatch, caplog, mem,
):
    """An aborted run on a NON-write-trusted origin writes nothing at all — and
    the withhold log must not claim otherwise.

    The log line reports only what its site DECIDES: that the partial answer
    is excluded. It must not claim the caller's task turn is kept, which is
    false here — a voice origin captures nothing.
    """
    import logging



    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="voice", cid="cid42")
    _FakeSDKClient.reset(response="partial answer", result_subtype="error_max_turns")

    with caplog.at_level(logging.WARNING, logger="tools"):
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            out = await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_aborted is True


    # Nothing was captured at all — not the answer, not the caller's turn.
    assert _captured(mem) == []

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
        "memory"
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
    monkeypatch, is_error, terminal_reason, mem,
):
    """INV-MEM-016 covers every way the CLI says the turn did not finish.

    `subtype` alone is not the verdict. A terminal result carrying
    `is_error=True` under `subtype="success"` is an API failure, and a
    non-completed `terminal_reason` is a turn the loop ended for another
    cause. Since cluster S (#709) these END the run as a typed caller fault
    RAISED before the answer's capture — the #568 refusal shape — so the
    half-finished answer is never captured; the raise reaches the delegation
    consumers' exception arms, which record the failure durably. The caller's
    request was captured before the run started, as for every run."""
    from error_kinds import ApiErrorTurn


    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_is_error=is_error, result_terminal_reason=terminal_reason)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        with pytest.raises(ApiErrorTurn):
            await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")


    assert _captured(mem) == ["Q1 cashflow?"]


@pytest.mark.parametrize("terminal_reason", [
    pytest.param("completed", id="control-completed"),
    pytest.param(None, id="control-no-terminal-reason"),
])
async def test_a_genuinely_completed_run_still_captures_both_turns(
    monkeypatch, terminal_reason, mem,
):
    """Controls for the caller-fault raise: a genuinely completed run, with
    and without a terminal reason (older CLIs report none), still captures
    both turns and raises nothing."""


    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_is_error=False, result_terminal_reason=terminal_reason)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_subtype == "success"
    assert out.run_aborted is False


    assert _captured(mem) == ["Q1 cashflow?", "partial answer"]


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
    monkeypatch, caplog, mem,
):
    """#710 (cluster S red case): `stop_reason="max_tokens"` on an otherwise
    completed result means the model did NOT finish the answer — the truncated
    text must be withheld from memory (audibly) while the caller's task turn
    is still submitted. The caller-facing verdict is untouched:
    `run_aborted` stays False. Fails pre-fix with 2 submitted items and 0
    warnings because the retain gate never reads `stop_reason`
    (tools.py:3017-3034)."""
    import logging



    cfg = _specialist_cfg(role="finance", token_budget=4000)
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


    assert _captured(mem) == ["Q1 cashflow?"]

    withheld = [r.getMessage() for r in caplog.records
                if "excluded from memory" in r.getMessage()]
    assert len(withheld) == 1


@pytest.mark.parametrize("stop_reason,expected_items,expect_warning", [
    pytest.param("tool_use", 1, True, id="tool-use-is-an-unfinished-answer"),
    pytest.param("a_stop_reason_nobody_listed", 1, True, id="unknown-stop-reason"),
    pytest.param("end_turn", 2, False, id="control-end-turn"),
    pytest.param("stop_sequence", 2, False, id="control-stop-sequence"),
    pytest.param(None, 2, False, id="control-legacy-absent"),
])
async def test_stop_reason_matrix(
    monkeypatch, caplog, stop_reason, expected_items, expect_warning, mem,
):
    """#710: the answer is admitted only under a completed stop reason —
    ALLOW-list direction, `None` legacy-completed; `tool_use` is excluded
    because on the memory-writing paths (`output_format=None`) a terminal
    tool call is an explicitly unfinished answer (seam round 1)."""
    import logging



    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_stop_reason=stop_reason)

    with caplog.at_level(logging.WARNING, logger="tools"):
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            out = await tools._run_delegated_agent(
                cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_aborted is False


    expected = ["Q1 cashflow?", "partial answer"][:expected_items]
    # Which items were captured is the point (#710), not their order: a drain
    # after a busy store can store the request after the answer.
    assert sorted(_captured(mem)) == sorted(expected)

    withheld = [r.getMessage() for r in caplog.records
                if "excluded from memory" in r.getMessage()]
    assert len(withheld) == (1 if expect_warning else 0)


async def test_malformed_stop_reason_withholds_the_answer(monkeypatch, mem):
    """#710/seam round 1: a malformed (non-string) stop reason is the
    fail-closed sentinel, never legacy None — exactly one caller item."""


    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response="partial answer", result_subtype="success",
        result_stop_reason=789)  # type: ignore[arg-type]

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        out = await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="")

    assert out.run_aborted is False


    assert _captured(mem) == ["Q1 cashflow?"]


@pytest.mark.parametrize("response,stop_reason", [
    pytest.param("", None, id="blank-task-blank-answer"),
    pytest.param("partial answer", "max_tokens", id="blank-task-withheld-answer"),
])
async def test_whitespace_only_task_submits_nothing(
    monkeypatch, response, stop_reason, mem,
):
    """#708: a whitespace-only task is BLANK — with no admissible answer
    nothing is captured at all. Kills the truthiness-for-strip mutant (seam
    round 2)."""


    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(
        response=response, result_subtype="success",
        result_stop_reason=stop_reason)

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(
            cfg, task_text="   ", context_text="")


    assert _captured(mem) == []

