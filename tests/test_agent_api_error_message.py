"""CLI API-error assistant messages must never reach a household (#568).

The Claude Code CLI reports API-level faults — including a safety refusal —
as an ordinary ``assistant`` message whose content is a **text block holding
the CLI's own user-facing error string**, with an envelope-level ``error``
field and, for a refusal, ``message.stop_reason == "refusal"``. Casa's
accumulator folded that text into the turn's reply, so the household read
CLI prose (an API Request ID, "Run --model to pick a different model") in
persona position.

The envelopes below are the measured wire shape, not invented ones:

* ``_MODEL_NOT_FOUND_ENVELOPE`` is a verbatim capture from
  ``claude --print --output-format stream-json --model
  claude-opus-9-does-not-exist`` against CLI 2.1.233.
* ``_REFUSAL_ENVELOPE`` is that same capture with the three fields the CLI's
  refusal builder sets on top of it (``error="invalid_request"``,
  ``message.stop_reason="refusal"``, ``message.stop_details``) and the
  refusal text its template produces.

Both are pushed through the **real SDK parser**
(``claude_agent_sdk._internal.message_parser.parse_message``) rather than a
hand-built ``AssistantMessage``, so a future SDK that stopped surfacing
``error``/``stop_reason`` would fail these tests instead of passing them.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)
from claude_agent_sdk._internal.message_parser import parse_message

import retry as retry_mod
from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from error_kinds import _USER_MESSAGES, ErrorKind
from mcp_registry import McpServerRegistry
from session_reg_helpers import RESIDENT_DIGEST, resident_prov, resident_role_id
from session_registry import SessionRegistry, build_scoped_session_key

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


# ---------------------------------------------------------------------------
# Measured wire envelopes
# ---------------------------------------------------------------------------

_MODEL_NOT_FOUND_TEXT = (
    "There's an issue with the selected model "
    "(claude-opus-9-does-not-exist). It may not exist or you may not have "
    "access to it. Run --model to pick a different model."
)

_MODEL_NOT_FOUND_ENVELOPE: dict = {
    "type": "assistant",
    "message": {
        "model": "claude-opus-9-does-not-exist",
        "id": "msg_011Ce4qBDoxebD92hDKRyQmB",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": _MODEL_NOT_FOUND_TEXT}],
        "stop_reason": "stop_sequence",
        "stop_sequence": None,
        "stop_details": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    },
    "error": "model_not_found",
    "is_api_error_message": True,
    "request_id": "req_011Ce4qBDoxebD92hDKRyQmB",
    "parent_tool_use_id": None,
    "session_id": "sid-scripted",
    "uuid": "e9c1e234-e17e-46c7-a5c2-12844046eb74",
}

_REFUSAL_TEXT = (
    "API Error: Claude Opus 5's safeguards flagged this message "
    "(https://www.anthropic.com/legal/aup). This sometimes happens with "
    "safe, normal conversations. Claude Code can't respond to this message "
    "with Claude Opus 5.\n"
    "Try rephrasing the request in a new session or change your model.\n"
    "Request ID: req_011Ce4qBDoxebD92hDKRyQmB"
)


def _refusal_envelope(*, parent_tool_use_id: str | None = None) -> dict:
    env = copy.deepcopy(_MODEL_NOT_FOUND_ENVELOPE)
    env["error"] = "invalid_request"
    env["parent_tool_use_id"] = parent_tool_use_id
    env["message"]["content"] = [{"type": "text", "text": _REFUSAL_TEXT}]
    env["message"]["stop_reason"] = "refusal"
    env["message"]["stop_details"] = {
        "type": "refusal", "category": "cyber", "explanation": None,
    }
    return env


def _parsed(envelope: dict) -> _SDKAssistantMessage:
    """Parse a wire envelope with the SDK's own parser."""
    msg = parse_message(copy.deepcopy(envelope))
    assert isinstance(msg, _SDKAssistantMessage)
    return msg


# ---------------------------------------------------------------------------
# Scripted transport double (same idiom as tests/test_agent_partial_streaming)
# ---------------------------------------------------------------------------


def _mk_result(sid: str, *, is_error: bool = False, result: str = "",
               stop_reason: str | None = None) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    m.is_error = is_error  # type: ignore[attr-defined]
    m.result = result  # type: ignore[attr-defined]
    m.stop_reason = stop_reason  # type: ignore[attr-defined]
    return m


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    """A NORMAL assistant message — no envelope error, as the SDK builds it."""
    return _SDKAssistantMessage(
        content=[_SDKTextBlock(text=text)], model="claude-sonnet-4-6",
    )


class ScriptedClient:
    def __init__(self, options, script: list, sid: str = "sid-scripted") -> None:
        self.options = options
        self._script = script
        self._sid = sid

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        return None

    async def receive_response(self):
        saw_result = False
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _SDKResultMessage):
                saw_result = True
            yield item
        if not saw_result:
            yield _mk_result(self._sid)


class QueuedScriptFactory:
    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)
        self.constructed = 0

    def __call__(self, options) -> ScriptedClient:
        self.constructed += 1
        script = self._scripts.pop(0) if self._scripts else []
        return ScriptedClient(options, script, sid=f"sid-attempt-{self.constructed}")


class WarmTurnClient(ScriptedClient):
    """One client, a script per TURN — what the pool actually does on warm
    reuse (a per-construction script would replay turn 1 forever)."""

    def __init__(self, options, turn_scripts: list[list], sid: str) -> None:
        super().__init__(options, [], sid=sid)
        self._turns = list(turn_scripts)

    async def receive_response(self):
        self._script = self._turns.pop(0) if self._turns else []
        async for item in super().receive_response():
            yield item


class WarmTurnFactory:
    """Constructs ONE client carrying every turn's script."""

    def __init__(self, turn_scripts: list[list]) -> None:
        self._turns = turn_scripts
        self.constructed = 0

    def __call__(self, options) -> WarmTurnClient:
        self.constructed += 1
        return WarmTurnClient(
            options, self._turns, sid=f"sid-attempt-{self.constructed}",
        )


class _FakeSdkClient:
    """A ``ClaudeSDKClient`` substitute for the non-pooled read loops
    (delegated specialist runs, ``_synthesize_answer``, the observer), which
    use ``async with ClaudeSDKClient(...)`` directly rather than the pool."""

    _script: list = []

    @classmethod
    def of(cls, *messages):
        return type("_ScriptedSdkClient", (cls,), {"_script": list(messages)})

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, text):
        return None

    async def receive_response(self):
        saw_result = False
        for item in type(self)._script:
            if isinstance(item, _SDKResultMessage):
                saw_result = True
            yield item
        if not saw_result:
            # Only synthesize a terminal result when the script has none — an
            # unconditional one would overwrite a scripted result's stop_reason
            # and make the second-carrier test vacuous.
            yield _mk_result("sid-delegated")


async def _noop():
    return None


async def _run_delegated():
    """Drive ``tools._run_delegated_agent`` with the delegation origin its
    contextvar contract requires (mirrors tests/test_delegate_to_agent.py)."""
    import tools

    try:
        from tests.test_delegate_to_agent import (
            _origin, _specialist_cfg, _with_origin,
        )
    except ImportError:
        from test_delegate_to_agent import (
            _origin, _specialist_cfg, _with_origin,
        )
    return await _with_origin(
        tools._run_delegated_agent(
            _specialist_cfg(), "question", "", resolution=None,
        ),
        _origin(),
    )


@contextmanager
def patch_retry_sleep():
    """Scope the sleep patch to retry.py's module-local ``asyncio`` — patching
    the global module turns the pool sweeper into a CPU spin (CLAUDE.md)."""
    sleep = AsyncMock()
    ns = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield sleep


def _make_agent(tmp_path, role: str = "butler") -> Agent:
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        # A resumable identity — without these the resume decision refuses
        # every stored entry and no turn ever resumes, which would make the
        # refused-session test below assert nothing (mirrors
        # tests/test_agent_pooling.py's fixture).
        role_id=resident_role_id(role),
        kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov(role),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _msg(text: str = "hi") -> BusMessage:
    return BusMessage(
        type=MessageType.REQUEST,
        source="telegram",
        target="butler",
        content=text,
        channel="telegram",
        context={"chat_id": "lr"},
    )


@pytest.fixture
async def agent_fixture(tmp_path):
    agent = _make_agent(tmp_path)
    yield agent
    await agent.aclose()


# ---------------------------------------------------------------------------
# The wire contract these tests stand on
# ---------------------------------------------------------------------------


def test_sdk_surfaces_the_envelope_error_and_refusal_stop_reason():
    """If this fails, the SDK stopped carrying the fields the fix reads and
    every suppression assertion below is vacuous."""
    plain = _parsed(_MODEL_NOT_FOUND_ENVELOPE)
    assert plain.error == "model_not_found"
    assert plain.stop_reason == "stop_sequence"
    assert plain.content[0].text == _MODEL_NOT_FOUND_TEXT

    refusal = _parsed(_refusal_envelope())
    assert refusal.error == "invalid_request"
    assert refusal.stop_reason == "refusal"

    # A normal assistant message carries no envelope error — the gate the fix
    # uses must not fire on a good answer.
    assert getattr(_mk_assistant("All good."), "error", None) is None


# ---------------------------------------------------------------------------
# Red cases
# ---------------------------------------------------------------------------


async def test_refusal_text_is_never_delivered_as_persona_text(
    agent_fixture, monkeypatch,
):
    """A refused turn must read as a refusal, not as the resident speaking
    the CLI's error string (which carries an API Request ID)."""
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        QueuedScriptFactory([[_parsed(_refusal_envelope())]]),
    )
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("what's the plan?"))

    assert out is not None
    body = out.content
    assert body == _USER_MESSAGES[ErrorKind.REFUSAL]
    assert "Request ID" not in body
    assert "safeguards flagged" not in body
    assert "anthropic.com/legal/aup" not in body


async def test_api_error_text_is_never_delivered_as_persona_text(
    agent_fixture, monkeypatch,
):
    """Same for a non-refusal API error — measured verbatim from the CLI."""
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        QueuedScriptFactory([[_parsed(_MODEL_NOT_FOUND_ENVELOPE)]]),
    )
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("what's the plan?"))

    assert out is not None
    assert out.content == _USER_MESSAGES[ErrorKind.API_ERROR]
    assert "--model" not in out.content


async def test_refusal_is_not_retried(agent_fixture, monkeypatch):
    """A deterministic decline must fail fast — retrying burns three attempts
    for a result that cannot change (#568)."""
    factory = QueuedScriptFactory([
        [_parsed(_refusal_envelope())],
        [_mk_assistant("second attempt")],
        [_mk_assistant("third attempt")],
    ])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("hi"))

    assert factory.constructed == 1
    assert out is not None
    assert out.content == _USER_MESSAGES[ErrorKind.REFUSAL]


async def test_partial_answer_before_a_refusal_is_not_passed_off_as_complete(
    agent_fixture, monkeypatch,
):
    """Text the model produced before the decline must not be delivered as a
    finished reply — that is the silent-truncation shape (#556's class)."""
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        QueuedScriptFactory([[
            _mk_assistant("Here is what I found so far."),
            _parsed(_refusal_envelope()),
        ]]),
    )
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("hi"))

    assert out is not None
    assert out.content == _USER_MESSAGES[ErrorKind.REFUSAL]
    assert "so far" not in out.content


async def test_result_stop_reason_refusal_is_the_second_carrier(
    agent_fixture, monkeypatch,
):
    """A refusal reported only on the ResultMessage (no api-error assistant
    message) must still classify — the assistant message is not the only
    carrier the CLI could use."""
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        QueuedScriptFactory([[
            _mk_result("sid-scripted", stop_reason="refusal"),
        ]]),
    )
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("hi"))

    assert out is not None
    assert out.content == _USER_MESSAGES[ErrorKind.REFUSAL]


async def test_a_refused_turn_does_not_leave_its_session_resumable(
    agent_fixture, monkeypatch,
):
    """Dropping the pool entry unbinds the client, not the conversation.

    The registry still named the session the refused turn was resuming, so the
    next turn on that key resumed the very conversation that was declined —
    with the declined message still in it. The CLI's own refusal advice is to
    start a new session.
    """
    agent = agent_fixture
    factory = WarmTurnFactory([
        [_mk_assistant("First answer.")],          # turn 1 registers a session
        [_parsed(_refusal_envelope())],            # turn 2 RESUMES it, refused
    ])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)

    with patch_retry_sleep():
        first = await agent.handle_message(_msg("hello"))
        assert first is not None and first.content == "First answer."
        key = build_scoped_session_key("telegram", "butler", "lr")
        assert agent._session_registry.get(key)["sdk_session_id"] == (
            "sid-attempt-1"
        )

        second = await agent.handle_message(_msg("and again?"))

    assert second is not None
    assert second.content == _USER_MESSAGES[ErrorKind.REFUSAL]
    entry = agent._session_registry.get(key) or {}
    assert not entry.get("sdk_session_id"), (
        "the refused conversation is still registered for resume"
    )


async def test_a_non_refusal_fault_keeps_the_session_resumable(
    agent_fixture, monkeypatch,
):
    """Discriminates the REFUSAL-only half of the clear's condition.

    A transient or configuration fault is no reason to discard a conversation
    the next turn could continue — only a decline poisons it.
    """
    agent = agent_fixture
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        WarmTurnFactory([
            [_mk_assistant("First answer.")],
            [_parsed(_MODEL_NOT_FOUND_ENVELOPE)],      # API_ERROR, not a refusal
        ]),
    )
    with patch_retry_sleep():
        await agent.handle_message(_msg("hello"))
        key = build_scoped_session_key("telegram", "butler", "lr")
        second = await agent.handle_message(_msg("again"))

    assert second is not None
    assert second.content == _USER_MESSAGES[ErrorKind.API_ERROR]
    assert agent._session_registry.get(key)["sdk_session_id"] == "sid-attempt-1"


async def test_delegated_result_only_refusal_also_ends_the_run(
    tmp_path, monkeypatch,
):
    """The second carrier, on the specialist path.

    The resident turn reads a refusal reported only on the terminal result;
    the delegated runner must too, or a declined specialist run returns an
    empty success that every one of its four consumers records as a completed
    delegation.
    """
    import tools
    from error_kinds import ApiErrorTurn

    monkeypatch.setattr(
        tools, "ClaudeSDKClient",
        _FakeSdkClient.of(_mk_result("sid-delegated", stop_reason="refusal")),
    )
    with pytest.raises(ApiErrorTurn) as caught:
        await _run_delegated()
    assert caught.value.kind is ErrorKind.REFUSAL


def test_every_sdk_read_loop_declares_how_it_handles_an_api_fault():
    """The inventory of read loops is itself pinned.

    Three review rounds each found one more loop that folded assistant text
    without reading the API-fault carriers — the recurrence was coverage, not
    mechanism. A NEW ``receive_response()`` call site now fails this test until
    it is declared here, the same way memory.md pins its recall-consumer
    inventory (INV-MEM-010's "declare how you frame a non-empty result").
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"

    # module -> (number of receive_response call sites, why each is safe).
    # The COUNT is load-bearing: keying on the filename alone would let a
    # second loop inside an already-declared module pass undeclared, which is
    # exactly how the fourth one was missed (Terra, closing round).
    declared = {
        # 1) run_turn_locked — reads BOTH carriers and raises ApiErrorTurn, so
        #    the pool drops the entry and the turn is classified
        #    (INV-TURN-007). 2) _interrupt_and_drain — drains an aborted turn
        #    to its terminal result and folds no text.
        "sdk_client_pool.py": (2, "raises ApiErrorTurn for the resident turn"),
        # 1) the delegated specialist run and 2) query_engager synthesis —
        #    both read both carriers and raise.
        "tools.py": (2, "raises from the delegated run and from synthesis"),
        # Suppresses the prose; the JSON parse then fails and the caller's
        # existing handler suppresses the interjection — the honest outcome
        # for an observer that could not decide.
        "observer.py": (1, "suppresses; an undecidable interjection is dropped"),
        # Suppresses the prose so it is never streamed into the engagement's
        # topic, and logs the kind. The engagement's TERMINAL kind is still
        # the generic one — tracked separately, see #595.
        "in_casa_driver.py": (1, "suppresses; empty-turn warning is the signal"),
    }

    found = {
        path.name: len(re.findall(r"\.receive_response\(", path.read_text()))
        for path in root.rglob("*.py")
        if re.search(r"\.receive_response\(", path.read_text())
    }

    undeclared = set(found) - set(declared)
    assert not undeclared, (
        f"new SDK read loop(s) in {sorted(undeclared)}: declare how each one "
        "handles an API-level fault (api_error_kind + result_api_error_kind) "
        "before adding it here — #568"
    )
    stale = set(declared) - set(found)
    assert not stale, f"declared but no longer reads the SDK: {sorted(stale)}"
    drifted = {
        name: (found[name], count)
        for name, (count, _why) in declared.items()
        if found[name] != count
    }
    assert not drifted, (
        f"read-loop count changed {drifted} (found, declared): a NEW loop in "
        "an already-declared module must declare how it handles an API-level "
        "fault — #568"
    )


async def test_normal_turn_is_untouched(agent_fixture, monkeypatch):
    """The gate must not fire on a good answer."""
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        QueuedScriptFactory([[_mk_assistant("All good.")]]),
    )
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("hi"))

    assert out is not None
    assert out.content == "All good."


async def test_delegated_specialist_api_error_is_not_its_answer(
    tmp_path, monkeypatch,
):
    """A specialist run the CLI ended with an API fault produced no answer.

    Folding the CLI's prose would make it the specialist's reply; folding
    nothing and reporting success would make an empty string its reply. The
    run must report as aborted so callers fail it.
    """
    import tools

    from error_kinds import ApiErrorTurn

    monkeypatch.setattr(
        tools, "ClaudeSDKClient",
        _FakeSdkClient.of(_parsed(_refusal_envelope())),
    )
    with pytest.raises(ApiErrorTurn) as caught:
        await _run_delegated()
    assert caught.value.kind is ErrorKind.REFUSAL


async def test_delegated_specialist_partial_text_before_an_api_error_is_dropped(
    tmp_path, monkeypatch,
):
    """Half an answer is not an answer — and must not be retained as one."""
    import tools

    from error_kinds import ApiErrorTurn

    retained: list = []
    monkeypatch.setattr(
        tools, "ClaudeSDKClient",
        _FakeSdkClient.of(
            _mk_assistant("Partial finding: "),
            _parsed(_refusal_envelope()),
        ),
    )
    monkeypatch.setattr(
        tools, "retain_delegated",
        lambda *a, **k: retained.append(a) or _noop(),
    )

    with pytest.raises(ApiErrorTurn):
        await _run_delegated()

    # The run raises before the retain, so no half-exchange is written to the
    # memory bank as a completed one.
    assert retained == []


async def test_delegate_to_agent_reports_an_api_error_as_a_failed_delegation(
    tmp_path, monkeypatch,
):
    """The non-voice branch consumed ``.text`` directly and completed the
    durable record as a success — so a refused specialist reached the
    narrating resident as an empty answer it was expected to present."""
    import tools
    from bus import MessageBus
    from specialist_registry import SpecialistRegistry

    try:
        from tests.test_delegate_to_agent import (
            _caller_cfg, _origin, _seed_specialist_dir, _with_origin,
        )
        from tests.test_specialist_registry import _use_synthetic_roles_dir
    except ImportError:
        from test_delegate_to_agent import (
            _caller_cfg, _origin, _seed_specialist_dir, _with_origin,
        )
        from test_specialist_registry import _use_synthetic_roles_dir

    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(
        str(specialists), tombstone_path=str(tmp_path / "del.json"),
    )
    reg.load()
    tools.init_tools(
        ChannelManager(), MessageBus(), reg,
        agent_role_map={"assistant": _caller_cfg(delegates=("finance",))},
    )
    monkeypatch.setattr(
        tools, "ClaudeSDKClient",
        _FakeSdkClient.of(_parsed(_refusal_envelope())),
    )

    failed: list = []
    real_fail = reg.fail_delegation

    async def _recording_fail(did, exc):
        failed.append(did)
        return await real_fail(did, exc)

    monkeypatch.setattr(reg, "fail_delegation", _recording_fail)

    result = await _with_origin(
        tools.delegate_to_agent.handler({
            "agent": "finance", "task": "draft invoice",
            "context": "", "mode": "sync",
        }),
        _origin(),
    )

    # The durable record must agree with what the caller was told.
    assert len(failed) == 1
    payload = result["content"][0]["text"]
    assert '"status": "error"' in payload
    assert ErrorKind.REFUSAL.value in payload
    assert "Request ID" not in payload
    assert "safeguards flagged" not in payload


async def test_async_delegation_completion_reports_an_api_error_as_failed(
    monkeypatch,
):
    """The async / degraded-sync completion callback consumed ``.text`` and
    notified ``status="ok"`` — so the delegating resident was handed an empty
    answer to narrate to the household as the specialist's own."""
    import tools
    from specialist_registry import DelegationRecord

    notified: list = []
    failed: list = []
    completed: list = []

    class _Registry:
        async def fail_delegation(self, did, exc):
            failed.append((did, exc))

        async def complete_delegation(self, did):
            completed.append(did)

        async def cancel_delegation(self, did):
            pass

    class _Bus:
        async def notify(self, msg):
            notified.append(msg)

    monkeypatch.setattr(tools, "_specialist_registry", _Registry())
    monkeypatch.setattr(tools, "_bus", _Bus())

    record = DelegationRecord(
        id="d-1", agent="finance", started_at=0.0,
        origin={"role": "assistant", "channel": "telegram", "chat_id": "lr"},
    )
    task: asyncio.Task = asyncio.create_task(_delegated_api_error_run())
    await asyncio.gather(task, return_exceptions=True)
    tools._attach_completion_callback(task, record)
    await asyncio.sleep(0)      # let the done-callback's tasks be scheduled
    await asyncio.sleep(0)

    assert completed == []
    assert [d for d, _ in failed] == ["d-1"]
    assert len(notified) == 1
    complete = notified[0].content
    assert complete.status == "error"
    assert complete.kind == ErrorKind.REFUSAL.value
    assert "Request ID" not in (complete.message or "")


async def _delegated_api_error_run():
    """A specialist run the CLI ended with a refusal — the runner raises."""
    from error_kinds import ApiErrorTurn

    raise ApiErrorTurn(ErrorKind.REFUSAL)


async def test_synthesize_answer_raises_instead_of_answering_with_cli_prose(
    monkeypatch,
):
    """``_synthesize_answer`` must not return "" for a faulted synthesis —
    an empty answer is reported by its caller as a successful empty answer."""
    import tools
    from error_kinds import ApiErrorTurn

    monkeypatch.setattr(
        tools, "ClaudeSDKClient",
        _FakeSdkClient.of(_parsed(_refusal_envelope())),
    )
    with pytest.raises(ApiErrorTurn) as caught:
        await tools._synthesize_answer("q", "ctx", 100)
    assert caught.value.kind is ErrorKind.REFUSAL


async def test_query_engager_reports_a_faulted_synthesis_as_unavailable(
    monkeypatch,
):
    """Not ``unknown`` — that word asserts the engager's memory holds nothing,
    a claim a faulted synthesis never established (the #201/#472 contract)."""
    import agent as agent_mod
    import tools
    from error_kinds import ApiErrorTurn

    class _Engagement:
        id = "eng-1"
        role_or_type = "configurator"
        origin = {"channel": "telegram"}

    async def _recall(*a, **k):
        return ("remembered context", 1)

    async def _boom(*a, **k):
        raise ApiErrorTurn(ErrorKind.REFUSAL)

    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(),
                        raising=False)
    monkeypatch.setattr(tools, "delegated_recall", _recall)
    monkeypatch.setattr(tools, "_synthesize_answer", _boom)

    token = tools.engagement_var.set(_Engagement())
    try:
        result = await tools.query_engager.handler(
            {"question": "what did they say?", "max_tokens": 500},
        )
    finally:
        tools.engagement_var.reset(token)

    payload = result["content"][0]["text"]
    assert '"unavailable"' in payload
    assert '"unknown"' not in payload


async def test_subagent_api_error_suppresses_text_without_failing_the_turn(
    agent_fixture, monkeypatch,
):
    """An API error scoped to a subagent (``parent_tool_use_id`` set) is not
    the main loop's verdict: its prose is still suppressed, but the resident's
    own answer stands."""
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        QueuedScriptFactory([[
            _parsed(_refusal_envelope(parent_tool_use_id="toolu_child")),
            _mk_assistant("I could not use that helper, but here is the answer."),
        ]]),
    )
    with patch_retry_sleep():
        out = await agent_fixture.handle_message(_msg("hi"))

    assert out is not None
    assert out.content == "I could not use that helper, but here is the answer."
    assert "Request ID" not in out.content
