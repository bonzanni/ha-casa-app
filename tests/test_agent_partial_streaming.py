"""Voice partial-message streaming — agent-side unit tests.

Spec: docs/superpowers/specs/2026-07-11-voice-partial-streaming-design.md
(§2 the change, AR-A pinned ``_cum()`` formula, AR-B channel guard [tested
separately in tests/test_voice_channel_sse.py / test_voice_channel_ws.py],
AR-E defensive parsing; §6 test matrix).

Drives ``Agent._process`` directly (not ``handle_message``) so a test can
supply its own recording ``on_token`` without needing a channel registered
in ``ChannelManager`` — mirrors tests/test_agent_process.py's on_token-
observing tests (e.g. ``test_on_token_replays_final_text_after_retry``).
The SDK boundary is scripted via ``sdk_client_pool._default_make_client``
(mirrors tests/test_agent_pooling.py) with fake clients that can yield
``StreamEvent`` instances alongside the usual canonical SDK messages.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    StreamEvent,
    TextBlock as _SDKTextBlock,
    ToolResultBlock as _SDKToolResultBlock,
    ToolUseBlock as _SDKToolUseBlock,
    UserMessage as _SDKUserMessage,
)

import retry as retry_mod
from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


# ---------------------------------------------------------------------------
# SDK-message helpers (SDK-shape-tolerant, mirror test_agent_pooling.py /
# test_agent_process.py's ``_mk_*`` idioms)
# ---------------------------------------------------------------------------


def _mk_text_block(text: str) -> _SDKTextBlock:
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant_content(blocks: list) -> _SDKAssistantMessage:
    try:
        return _SDKAssistantMessage(content=blocks)
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = blocks  # type: ignore[attr-defined]
        return m


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    return _mk_assistant_content([_mk_text_block(text)])


def _mk_assistant_tool_use(name: str, tool_id: str) -> _SDKAssistantMessage:
    """An AssistantMessage carrying ONLY a ToolUseBlock — no TextBlock."""
    block = _SDKToolUseBlock(id=tool_id, name=name, input={})
    return _mk_assistant_content([block])


def _mk_user_tool_result(tool_id: str, content: str) -> _SDKUserMessage:
    block = _SDKToolResultBlock(tool_use_id=tool_id, content=content)
    try:
        return _SDKUserMessage(content=[block])
    except TypeError:
        m = _SDKUserMessage.__new__(_SDKUserMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid: str, usage: dict | None = None) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    m.is_error = False  # type: ignore[attr-defined]
    m.result = ""  # type: ignore[attr-defined]
    if usage is not None:
        m.usage = usage  # type: ignore[attr-defined]
    return m


def _mk_stream_event(text: str, *, session_id: str = "sid-scripted") -> StreamEvent:
    """A StreamEvent carrying one text_delta content_block_delta payload —
    the only shape agent.py's on_message acts on (design §2 point 2)."""
    return StreamEvent(
        uuid="ev",
        session_id=session_id,
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _mk_stream_event_raw(event, *, session_id: str = "sid-scripted") -> StreamEvent:
    """A StreamEvent with an arbitrary/malformed ``event`` payload (AR-E)."""
    return StreamEvent(uuid="ev", session_id=session_id, event=event)


# ---------------------------------------------------------------------------
# Scripted transport double
# ---------------------------------------------------------------------------


class ScriptedClient:
    """Feeds a pre-built ``script`` of SDK messages through
    ``receive_response()``.

    An ``Exception`` entry in the script is RAISED from inside the
    generator at that point — a mid-stream fault landing after some
    messages were already delivered (AR-B/agent retry test below). A
    trailing ``ResultMessage`` is auto-appended if the script doesn't end
    with one, so every turn that doesn't fault terminates cleanly.
    """

    def __init__(self, options, script: list, sid: str = "sid-scripted") -> None:
        self.options = options
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []
        self._script = script
        self._sid = sid

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

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
    """``make_client(options)`` factory popping one script per construction.

    Lets a test script DISTINCT attempts (e.g. attempt 1 raises mid-stream,
    attempt 2 succeeds) the way the pool's cold-connect-per-attempt
    behaviour actually exercises them (a failed entry is dropped, so the
    next ``turn()`` cold-connects again — see sdk_client_pool.py's
    ``_drop``/AR-5).
    """

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)
        self.constructed = 0
        self.clients: list[ScriptedClient] = []

    def __call__(self, options) -> ScriptedClient:
        self.constructed += 1
        script = self._scripts.pop(0) if self._scripts else []
        c = ScriptedClient(options, script, sid=f"sid-attempt-{self.constructed}")
        self.clients.append(c)
        return c


# ---------------------------------------------------------------------------
# Retry-sleep patch — copied idiom from tests/test_agent_process.py.
#
# Patching the GLOBAL asyncio module would turn the pool sweeper's
# ``while True: await asyncio.sleep(interval)`` background task into a
# tight CPU spin (its AsyncMock records every call) -> unbounded RSS and an
# OOM that takes down the whole pytest process, per CLAUDE.md's memory-cage
# note. Only retry.py's MODULE-LOCAL ``asyncio`` reference is replaced.
# ---------------------------------------------------------------------------


@contextmanager
def patch_retry_sleep():
    sleep = AsyncMock()
    ns = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield sleep


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def _make_agent(tmp_path, role: str = "butler") -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _msg(channel: str, chat_id: str, text: str = "hi") -> BusMessage:
    return BusMessage(
        type=MessageType.REQUEST,
        source=channel,
        target="butler",
        content=text,
        channel=channel,
        context={"chat_id": chat_id},
    )


@pytest.fixture
async def agent_fixture(tmp_path):
    agent = _make_agent(tmp_path)
    yield agent
    # Teardown: close the pool so the per-test sdk-pool-sweeper task and any
    # warm ScriptedClients die with the test (mirrors test_agent_pooling.py).
    await agent.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_incremental_partial_deltas_call_on_token_with_growing_cumulative(
    agent_fixture, monkeypatch,
):
    """StreamEvent text_deltas drive on_token with a growing cumulative
    string; the canonical fold that matches the last partial exactly is
    deduped (no 3rd call) — spec §2 point 2 / §5 risk table row 1."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("Hello"),
        _mk_stream_event(" there"),
        _mk_assistant("Hello there"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == ["Hello", "Hello there"]
    assert text == "Hello there"


async def test_no_duplicate_emit_when_canonical_matches_last_partial(
    agent_fixture, monkeypatch,
):
    """§5 risk table: "Duplicate text spoken" mitigation — on_token only
    fires when cumulative grew/changed; the canonical fold's cumulative
    equalling the last partial's cumulative must NOT re-emit."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("All "),
        _mk_stream_event("good."),
        _mk_assistant("All good."),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    await agent._process(_msg("voice", "lr", "status"), on_token=on_token)

    assert seen == ["All ", "All good."]
    assert seen.count("All good.") == 1


async def test_ar_a_joiner_present_in_message_two_first_partial(
    agent_fixture, monkeypatch,
):
    """AR-A: the pinned ``_cum()`` formula puts the "\\n\\n" joiner in
    message 2's FIRST partial emission after a tool-use fold (msg1 -> tool
    -> msg2), exactly matching what the canonical fold will eventually
    produce."""
    factory = QueuedScriptFactory([[
        _mk_assistant("Let me check."),                   # msg1 canonical fold
        _mk_assistant_tool_use("get_time", "tool-1"),      # tool_use only, no text
        _mk_user_tool_result("tool-1", "12:00"),            # tool result
        _mk_stream_event("It's"),                            # msg2 first partial
        _mk_stream_event(" sunny."),                          # msg2 second partial
        _mk_assistant("It's sunny."),                         # msg2 canonical fold
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "weather?"), on_token=on_token)

    assert seen[0] == "Let me check."
    msg2_emissions = seen[1:]
    assert msg2_emissions, "expected on_token calls for message 2's partials"
    for call in msg2_emissions:
        assert call.startswith("Let me check.\n\n"), (
            f"AR-A: every emission after msg1's fold must carry the "
            f"joiner; got {call!r}"
        )
    assert msg2_emissions[0] == "Let me check.\n\nIt's"
    assert msg2_emissions[-1] == "Let me check.\n\nIt's sunny."
    assert text == "Let me check.\n\nIt's sunny."


async def test_ar_b_retry_attempt_two_emissions_start_fresh(
    agent_fixture, monkeypatch,
):
    """AR-B/agent: script deltas, then raise a retryable fault, then a
    clean attempt. ``_make_on_message`` builds a FRESH ``state`` per
    attempt (``retry_sdk_call`` invokes the ``_attempt_pooled_turn``
    closure anew on each try, and its first line calls
    ``self._make_on_message(on_token)``) — so attempt 2's cumulative never
    carries attempt 1's text, even though the channel's own on_token is
    the SAME callable across both attempts (the channel-side guard for
    THIS scenario is unit-tested separately in
    tests/test_voice_channel_sse.py / test_voice_channel_ws.py)."""
    RetryableError = type("CLIConnectionError", (RuntimeError,), {})
    factory = QueuedScriptFactory([
        [
            _mk_stream_event("Attempt one partial "),
            RetryableError("upstream reset"),
        ],
        [
            _mk_stream_event("Attempt two "),
            _mk_stream_event("reply."),
            _mk_assistant("Attempt two reply."),
        ],
    ])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    with patch_retry_sleep():
        text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert text == "Attempt two reply."
    assert factory.constructed == 2
    # attempt 1 DID emit a partial before the fault.
    assert any(s.startswith("Attempt one") for s in seen)
    # attempt 2's emissions are exactly its own growing cumulative — none
    # regress into / extend attempt-1 text.
    attempt_two_calls = [s for s in seen if s.startswith("Attempt two")]
    assert attempt_two_calls == ["Attempt two ", "Attempt two reply."]
    assert not any("Attempt one" in s for s in attempt_two_calls)


async def test_canonical_longer_divergent_correction_wins(agent_fixture, monkeypatch):
    """§5 risk table: canonical text != accumulated partials (SDK
    correction) — the canonical text wins at fold time via ``_cum()``; the
    corrected, divergent text is what on_token ultimately reports, and it
    differs from the last partial actually emitted."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("Hel"),
        _mk_assistant("Completely different final answer."),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen[0] == "Hel"
    assert seen[-1] == "Completely different final answer."
    assert text == "Completely different final answer."


async def test_canonical_shrinking_correction_is_safe(agent_fixture, monkeypatch):
    """A canonical fold SHORTER than the accumulated partial must not
    crash; the shorter, corrected text is what on_token ultimately
    reports — the canonical text always wins, regardless of direction."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("Hello there my friend, "),
        _mk_stream_event("this is a very long partial answer"),
        _mk_assistant("Hi."),  # SDK correction: much shorter than the partial
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert text == "Hi."
    assert seen[-1] == "Hi."


async def test_malformed_stream_event_ignored_without_aborting_turn(
    agent_fixture, monkeypatch, caplog,
):
    """AR-E: the CLI can forward raw ``error`` events with no ``delta``
    key, unrelated event types, ``input_json_delta`` (tool-use, not text),
    and even a StreamEvent whose ``event`` payload isn't a dict at all —
    none of these may abort the turn or emit a bogus on_token call. The
    genuinely malformed shape (``event`` not a dict) exercises the
    defensive try/except and logs a warning instead of raising."""
    factory = QueuedScriptFactory([[
        _mk_stream_event_raw({"type": "message_start"}),
        _mk_stream_event_raw({
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        }),
        _mk_stream_event_raw({"type": "content_block_delta"}),  # no "delta" key
        _mk_stream_event_raw({
            "type": "error", "error": {"type": "overloaded_error", "message": "boom"},
        }),
        _mk_stream_event_raw(None),
        StreamEvent(uuid="bad", session_id="s", event="not-a-dict"),
        _mk_stream_event("Still works."),
        _mk_assistant("Still works."),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    with caplog.at_level(logging.WARNING, logger="agent"):
        text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert text == "Still works."
    assert seen == ["Still works."]
    assert any(
        "stream_event dispatch failed" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


async def test_include_partial_messages_voice_only(agent_fixture, monkeypatch):
    """§2.1: ``include_partial_messages`` is derived from ``channel`` — True
    only for voice; a telegram turn's captured options must not set it."""
    factory = QueuedScriptFactory([
        [_mk_assistant("hi")],
        [_mk_assistant("hi2")],
    ])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture

    await agent._process(_msg("voice", "lr", "hi"))
    await agent._process(_msg("telegram", "123", "hi"))

    assert factory.clients[0].options.include_partial_messages is True
    assert factory.clients[1].options.include_partial_messages is False


# ---------------------------------------------------------------------------
# #666 — the silence sentinel is never handed to the token stream
# (INV-TURN-009; red cases specified by terra, redcase round 1).
# ---------------------------------------------------------------------------


async def test_sentinel_only_fold_emits_nothing(agent_fixture, monkeypatch):
    """INV-TURN-009: a turn whose canonical fold is nothing but the literal
    sentinel hands NOTHING to on_token. At the base the fold emits
    ``'<silent/>'`` and Telegram posts it as a real message that the final
    gate then never supersedes."""
    factory = QueuedScriptFactory([[_mk_assistant("<silent/>")]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == []
    assert text == "<silent/>"


async def test_repeated_and_padded_sentinel_folds_emit_nothing(
    agent_fixture, monkeypatch,
):
    """The hold reuses ``_strips_to_silence``, so every form that whole-output
    silence already covers — whitespace padding, repetition — is held too."""
    factory = QueuedScriptFactory([[
        _mk_assistant("\n  <silent/>  \n"),
        _mk_assistant("<silent/>"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == []


async def test_voice_sentinel_fragments_emit_nothing(agent_fixture, monkeypatch):
    """The voice StreamEvent path delivers the sentinel in fragments, and a
    fragment does NOT strip to silence (``_strips_to_silence('<sil')`` is
    False at the base). The partial path's hold is therefore prefix-aware:
    every cumulative that could still become sentinel-only is held."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("<"),
        _mk_stream_event("sil"),
        _mk_stream_event("ent/>"),
        _mk_assistant("<silent/>"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == []
    assert text == "<silent/>"


async def test_sentinel_prefix_that_diverges_releases_the_whole_cumulative(
    agent_fixture, monkeypatch,
):
    """A cumulative that merely LOOKED like a sentinel on its way in is
    released in full the moment it diverges — the whole ``_cum()``, not the
    diverging tail. At the base the held prefix leaks first."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("<sil"),
        _mk_stream_event("ly good"),
        _mk_assistant("<silly good"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == ["<silly good"]
    assert text == "<silly good"


async def test_g3_recant_releases_the_exact_cumulative_sentinel_included(
    agent_fixture, monkeypatch,
):
    """The G-3 recant contract: a sentinel followed by real prose delivers the
    WHOLE text, literal included. The hold must release the exact ``_cum()``
    — a stripped variant would make the final delivery re-insert what the
    stream hid, and would break the AR-A formula."""
    factory = QueuedScriptFactory([[
        _mk_assistant("<silent/>"),
        _mk_assistant("Actually, meeting at 3pm."),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == ["<silent/>\n\nActually, meeting at 3pm."]
    assert text == "<silent/>\n\nActually, meeting at 3pm."


async def test_held_sentinel_does_not_survive_into_the_next_attempt(
    agent_fixture, monkeypatch,
):
    """AR-E: a fresh ``state`` per attempt. Attempt 1 folds the sentinel (held)
    and then faults; attempt 2 answers normally, and its emissions are its own
    — nothing of attempt 1 is carried, released or re-held."""
    RetryableError = type("CLIConnectionError", (RuntimeError,), {})
    factory = QueuedScriptFactory([
        [
            _mk_assistant("<silent/>"),
            RetryableError("upstream reset"),
        ],
        [
            _mk_stream_event("Attempt two "),
            _mk_stream_event("reply."),
            _mk_assistant("Attempt two reply."),
        ],
    ])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    with patch_retry_sleep():
        text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert factory.constructed == 2
    assert seen == ["Attempt two ", "Attempt two reply."]
    assert text == "Attempt two reply."


async def test_divergent_canonical_correction_to_silence_emits_only_the_prose(
    agent_fixture, monkeypatch,
):
    """The NARROW scope of INV-TURN-009, pinned so it cannot be re-inflated.

    A voice partial can stream non-silent provisional prose which the canonical
    fold then REPLACES with the sentinel (a divergent canonical correction —
    ``test_canonical_shrinking_correction_is_safe`` above pins that path). The
    prose has already been spoken and cannot be un-spoken; what the hold
    guarantees is that the correction to silence adds NO further emission. At
    the base the fold additionally emits the literal sentinel."""
    factory = QueuedScriptFactory([[
        _mk_stream_event("Visible provisional answer."),
        _mk_assistant("<silent/>"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == ["Visible provisional answer."]
    assert text == "<silent/>"


async def test_bare_sentinel_prefix_fold_is_released_not_held(
    agent_fixture, monkeypatch,
):
    """The canonical fold uses the STRICT predicate, not the prefix-aware one.

    A fold cumulative of ``'<sil'`` is not silence: ``handle_message``'s gate
    DELIVERS it, so the stream must not hold it — and on voice, whose ``send()``
    is a no-op with ``delivers_final_text = False``, holding it would lose the
    message outright. This also kills the "record a held cumulative in
    last_emitted" mutant: the held partial would dedup the fold's release away
    and ``seen`` would be empty. (Passes at the base — a regression pin on the
    fix's shape, not a red case.)"""
    factory = QueuedScriptFactory([[
        _mk_stream_event("<sil"),
        _mk_assistant("<sil"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == ["<sil"]
    assert text == "<sil"


async def test_a_sentinel_fragment_with_content_behind_it_is_released(
    agent_fixture, monkeypatch,
):
    """The incomplete prefix must be the SUFFIX of the cumulative.

    ``"<sil<silent/>"`` can never become sentinel-only — the fragment has
    content behind it — so the hold must release at that delta rather than wait
    for the next one. The prefix-aware predicate consumes only LEADING complete
    sentinels for exactly this reason; a variant that deleted sentinels anywhere
    would hold this cumulative and delay the device by a whole delta. (Added
    after the accepted red cases, as a mutation pin: nothing else distinguishes
    the two predicate shapes.)"""
    factory = QueuedScriptFactory([[
        _mk_stream_event("<sil"),
        _mk_stream_event("<silent/>"),
        _mk_stream_event("x"),
        _mk_assistant("<sil<silent/>x"),
    ]])
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    agent = agent_fixture
    seen: list[str] = []

    async def on_token(text: str) -> None:
        seen.append(text)

    text = await agent._process(_msg("voice", "lr", "hi"), on_token=on_token)

    assert seen == ["<sil<silent/>", "<sil<silent/>x"]
    assert text == "<sil<silent/>x"
