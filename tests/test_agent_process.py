"""Integration tests for Agent._process — memory wiring + channel_context."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import retry as retry_mod
from agent import Agent, ResumeDecision
from bus import BusMessage, MessageType
from channels import ChannelManager, DeliveryOutcome
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from semantic_memory import SemanticMemory
from session_registry import SessionRegistry, build_scoped_session_key
from session_reg_helpers import (
    RESIDENT_DIGEST,
    STUB_SPEAKER_PROV,
    STUB_USER_PROV,
    resident_prov,
    resident_role_id,
)

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
    ToolResultBlock as _SDKToolResultBlock,
    ToolUseBlock as _SDKToolUseBlock,
    UserMessage as _SDKUserMessage,
)

from error_kinds import VoiceToolLoopError
from voice_turn_guard import VoiceTurnGuard

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@contextmanager
def patch_retry_sleep():
    """Make retry's backoff instant WITHOUT touching the global asyncio.

    Patching ``retry.asyncio.sleep`` mutates the shared ``asyncio`` module
    object process-wide. Because every pooled turn starts the
    SdkClientPool sweeper (``while True: await asyncio.sleep(interval)`` in a
    background task), an instant-return global sleep turns that loop into a
    tight CPU spin whose AsyncMock records every call → unbounded RSS
    (~23 GB) + OOM. Replacing retry's *module-local* ``asyncio`` reference
    with a namespace carrying only the two attributes retry.py uses leaves
    the sweeper's ``asyncio.sleep`` real. Yields the sleep AsyncMock.
    """
    sleep = AsyncMock()
    ns = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield sleep


def _mk_text_block(text: str) -> _SDKTextBlock:
    """Instantiate whatever TextBlock shape the installed SDK uses."""
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_tool_use(
    tool_id: str, name: str, tool_input: dict | None = None,
) -> _SDKAssistantMessage:
    block = _SDKToolUseBlock(
        id=tool_id, name=name, input=tool_input or {},
    )
    try:
        return _SDKAssistantMessage(
            content=[block], model="claude-haiku-4-5",
        )
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_tool_result(
    tool_id: str, *, is_error: bool, text: str,
) -> _SDKUserMessage:
    block = _SDKToolResultBlock(
        tool_use_id=tool_id, content=text, is_error=is_error,
    )
    try:
        return _SDKUserMessage(content=[block])
    except TypeError:
        m = _SDKUserMessage.__new__(_SDKUserMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid: str, usage: dict[str, int] | None = None) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    if usage is not None:
        m.usage = usage  # type: ignore[attr-defined]
    return m


class FakeSemanticMemory(SemanticMemory):
    """SemanticMemory double for the §4.3 read path.

    ``profile`` returns the mental-model overlay (rendered as <peer_overlay>);
    ``recall`` returns the query-specific facts digest (rendered as
    <memory_context>). Calls are recorded so tests can assert the channel-aware
    load contract (overlay at fresh-session start; recall text-only)."""

    def __init__(self, overlay: str = "", facts: str = "", provenance=None) -> None:
        self._overlay = overlay
        self._facts = facts
        self._provenance = provenance
        self.profile_calls: list[str] = []
        self.recall_calls: list[dict] = []
        self.retain_calls: list[tuple] = []

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append((bank, items, async_))

    async def recall(
        self, bank, query, *, tags, max_tokens,
        types=("world", "experience", "observation"),
        tags_match="any", budget="mid",
    ):
        self.recall_calls.append({
            "bank": bank, "query": query, "tags": tags,
            "max_tokens": max_tokens, "budget": budget,
        })
        return self._facts

    async def recall_items(
        self, bank, query, *, tags, max_tokens, clearance,
        types=("world", "experience", "observation"),
        tags_match="any", budget="mid",
    ):
        self.recall_calls.append({
            "bank": bank, "query": query, "tags": tags,
            "max_tokens": max_tokens, "budget": budget, "clearance": clearance,
        })
        if not self._facts:
            return ()
        from personality_types import RecallHit
        return (RecallHit(
            text=self._facts, memory_type="world", sensitivity="friends",
            application_tags=(), provenance=self._provenance, backend_id="b1",
            document_id=None, chunk_id=None, source_fact_ids=None, metadata=None,
            context=None, score=None,
        ),)

    async def profile(self, bank):
        self.profile_calls.append(bank)
        return self._overlay


class FakeClient:
    """Minimal ClaudeSDKClient substitute with per-attempt behaviour.

    ``failure_schedule``: list of exceptions (or None) consumed one per
    construction. A None entry means "this attempt succeeds and yields
    the usual response". Reset before each test via
    ``FakeClient.reset()``.
    """

    captured_options = None
    response_text: str = "pong"
    failure_schedule: list[Exception | None] = []
    attempts: int = 0
    usage: dict[str, int] | None = None

    @classmethod
    def reset(cls) -> None:
        cls.captured_options = None
        cls.response_text = "pong"
        cls.failure_schedule = []
        cls.attempts = 0
        cls.usage = None

    def __init__(self, options):
        FakeClient.captured_options = options
        FakeClient.attempts += 1
        # The exception (if any) for this attempt is popped in query().
        if FakeClient.failure_schedule:
            self._scheduled = FakeClient.failure_schedule.pop(0)
        else:
            self._scheduled = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._last = text
        if self._scheduled is not None:
            raise self._scheduled

    async def receive_response(self):
        yield _mk_assistant(FakeClient.response_text)
        yield _mk_result("sdk-sid-1", usage=FakeClient.usage)


class ScriptedToolClient:
    """SDK client double that consumes one SDK-message script per attempt."""

    scripts: list[list[Any]] = []
    instances: list["ScriptedToolClient"] = []

    @classmethod
    def reset(cls, *scripts: list[Any]) -> None:
        cls.scripts = [list(script) for script in scripts]
        cls.instances = []

    def __init__(self, _options):
        self._script = self.scripts.pop(0)
        self.disconnected = False
        self.instances.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True

    async def query(self, _text):
        return None

    async def receive_response(self):
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            yield item


def _make_agent(
    tmp_path,
    role: str = "assistant",
    semantic_memory: SemanticMemory | None = None,
) -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(
            token_budget=1000,
            read_strategy="per_turn",
        ),
        # Task 9: a real resident identity triple so session registration and
        # the resume gate operate on canonical {role_id, binding_digest}.
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
        semantic_memory=semantic_memory or FakeSemanticMemory(),
    )


def _msg(channel: str, chat_id: str, text: str = "ping") -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN,
        source="telegram" if channel == "telegram" else channel,
        target="assistant",
        content=text,
        channel=channel,
        context={"chat_id": chat_id},
    )


async def test_resident_options_resolve_mcp_with_role_grants_and_skill_policy(
    tmp_path,
):
    agent = _make_agent(tmp_path, role="butler")
    agent.config.tools = ToolsConfig(
        allowed=["Skill", "mcp__casa-framework__get_schedule"],
        permission_mode="acceptEdits",
        skills="none",
    )
    agent.config.mcp_server_names = ["casa-framework"]
    agent._mcp_registry.register_sdk_factory(
        "casa-framework",
        lambda role, grants: {
            "type": "sdk",
            "instance": object(),
            "resolved_role": role,
            "resolved_grants": grants,
        },
    )

    options = await agent._build_options(
        channel="voice",
        channel_key="voice-butler-house",
        is_fresh=True,
        resume_sid=None,
        user_text="what is next?",
    )

    server = options.mcp_servers["casa-framework"]
    assert server["resolved_role"] == "butler"
    assert server["resolved_grants"] == frozenset({
        "mcp__casa-framework__get_schedule",
    })
    assert options.allowed_tools == [
        "mcp__casa-framework__get_schedule",
    ]
    assert options.skills is None


async def test_session_id_is_channel_plus_role(tmp_path):
    # §4.3: the read path no longer fans out per-scope Honcho sessions; it
    # recalls once against the role's bank.
    sem = FakeSemanticMemory(facts="recall digest")
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    # Fresh telegram session → one bank recall keyed to the shared casa bank.
    assert len(sem.recall_calls) == 1
    assert sem.recall_calls[0]["bank"] == "casa"
    entry = agent._session_registry.get(
        build_scoped_session_key("telegram", "assistant", "123"),
    )
    assert entry is not None


class _StubTelegramChannel:
    """Minimal channel double exercising the L7 handle_message delivery path."""

    name = "telegram"

    def __init__(self) -> None:
        self.send = AsyncMock()
        self.send_response = AsyncMock()
        self.finalize_stream = AsyncMock()
        self.finalize_response_stream = AsyncMock()
        self.turn_finished = AsyncMock()

    def create_on_token(self, _context):
        async def _on_token(_text: str) -> None:
            return None
        return _on_token


class TestSilentTurnTeardown:
    """L7 (v0.52.0): a turn that strips to empty / `<silent/>` never calls
    send()/finalize_stream(), so handle_message must call the channel's
    turn_finished() teardown hook (which stops the Telegram typing loop)."""

    async def test_silent_turn_calls_turn_finished(self, tmp_path):
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubTelegramChannel()
        agent._channel_manager.register(stub)
        with patch.object(agent, "_process", AsyncMock(return_value="<silent/>")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert stub.turn_finished.call_count == 1

    async def test_delivered_turn_does_not_call_turn_finished(self, tmp_path):
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubTelegramChannel()
        agent._channel_manager.register(stub)
        with patch.object(agent, "_process", AsyncMock(return_value="real reply")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        # v0.70.0: a successful streamed turn (error_kind is None) routes through
        # the response-provenant finalizer, NOT the plain one.
        assert stub.finalize_response_stream.call_count == 1
        assert stub.finalize_stream.call_count == 0
        assert stub.turn_finished.call_count == 0


class TestResponseProvenanceRouting:
    """v0.70.0: rich-text renders only genuine agent responses; error/system
    text stays on the plain finalizer."""

    async def test_success_streamed_turn_uses_response_finalizer(self, tmp_path):
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubTelegramChannel()
        agent._channel_manager.register(stub)
        with patch.object(agent, "_process", AsyncMock(return_value="ok reply")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_count == 1
        assert stub.finalize_stream.call_count == 0

    async def test_error_turn_uses_plain_finalizer(self, tmp_path):
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubTelegramChannel()
        agent._channel_manager.register(stub)
        with patch.object(
            agent, "_process", AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        # error text (error_kind set) must NOT be rich-rendered
        assert stub.finalize_stream.call_count == 1
        assert stub.finalize_response_stream.call_count == 0


class TestHealthNoticeDeliveryConfirmation:
    """#551: repeat suppression lives in plugin_health's decaying memo, not in
    a per-Agent flag. A delivery that RAISED certainly showed nothing, so it
    releases the cooldown immediately (#349's guarantee, preserved)."""

    def _agent_with_notice(self, tmp_path, monkeypatch, notice):
        import plugin_health
        plugin_health._notice_memo.clear()
        monkeypatch.setattr(
            plugin_health, "render_notice",
            lambda role, path=plugin_health.HEALTH_PATH: notice,
        )
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubTelegramChannel()
        agent._channel_manager.register(stub)
        return agent, stub

    async def test_failed_delivery_releases_the_cooldown(
        self, tmp_path, monkeypatch,
    ):
        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )
        stub.finalize_response_stream.side_effect = RuntimeError("channel down")

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            with pytest.raises(RuntimeError):
                await agent.handle_message(_msg("telegram", "123", "hi"))

        # Channel recovers: the SAME notice is delivered on the next turn,
        # with no cooldown wait — the raise proved it was never shown.
        stub.finalize_response_stream.side_effect = None
        with patch.object(agent, "_process", AsyncMock(return_value="again")):
            await agent.handle_message(_msg("telegram", "123", "hi"))

        delivered = stub.finalize_response_stream.call_args[0][0]
        assert delivered.startswith("PLUGIN-DEGRADED")
        assert delivered.endswith("again")

    async def test_notice_survives_a_delivery_that_sent_nothing(
        self, tmp_path, monkeypatch,
    ):
        """INV-TG-006 / #556: the delivery RETURNED, having sent nothing.

        A Telegram reconnect nulls the PTB app mid-turn; every delivery method
        then returns normally after zero Bot API calls. Before this fix that
        was indistinguishable from success, so the notice was consumed without
        ever being displayed — and the operator had no way to ask for it.
        """
        from channels import DeliveryOutcome

        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )
        stub.finalize_response_stream.return_value = (
            DeliveryOutcome.NOT_DELIVERED)

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0].startswith(
            "PLUGIN-DEGRADED",
        )

        # Channel recovers. The notice must be offered again AT ONCE — not
        # after the one-hour cooldown — because it was never shown.
        stub.finalize_response_stream.return_value = DeliveryOutcome.DELIVERED
        with patch.object(agent, "_process", AsyncMock(return_value="again")):
            await agent.handle_message(_msg("telegram", "123", "hi"))

        delivered = stub.finalize_response_stream.call_args[0][0]
        assert delivered.startswith("PLUGIN-DEGRADED")
        assert delivered.endswith("again")

    async def test_head_delivered_then_raise_does_not_repeat_the_notice(
        self, tmp_path, monkeypatch,
    ):
        """#556: a raise is only a reliable negative when NOTHING was shown.

        A multi-page reply whose page 1 landed HAS displayed the notice; if a
        later page raises, re-offering it next turn nags the operator about
        something they already read. The head stamp is what tells the two
        apart, because a raising method returns no outcome at all.
        """
        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )

        async def _head_then_raise(text, context, on_token):
            context["_delivery_head_sent"] = True     # page 1 landed
            raise RuntimeError("page 3 failed")

        stub.finalize_response_stream.side_effect = _head_then_raise
        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            with pytest.raises(RuntimeError):
                await agent.handle_message(_msg("telegram", "123", "hi"))

        # Next turn: unprefixed, because the operator has already seen it.
        stub.finalize_response_stream.side_effect = None
        with patch.object(agent, "_process", AsyncMock(return_value="second")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0] == "second"

    async def test_an_ambiguous_transport_failure_does_not_repeat_the_notice(
        self, tmp_path, monkeypatch,
    ):
        """Sol + Terra diff r1: the regression this batch nearly shipped.

        A lost acknowledgement (TimedOut) on a final edit is not evidence that
        the edit failed — Telegram may have applied it, in which case the
        operator has read the notice. Reporting NOT_DELIVERED there re-offered
        it on the next turn, which the pre-contract code did NOT do, because it
        swallowed the error and returned nothing.
        """
        from channels import DeliveryOutcome

        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )
        stub.finalize_response_stream.return_value = DeliveryOutcome.UNKNOWN

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0].startswith(
            "PLUGIN-DEGRADED",
        )

        # Not repeated: the notice may well have been displayed.
        with patch.object(agent, "_process", AsyncMock(return_value="second")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0] == "second"

    async def test_channel_off_the_contract_keeps_todays_behavior(
        self, tmp_path, monkeypatch,
    ):
        """UNKNOWN is not NOT_DELIVERED. A channel that returns None has not
        opted in; treating that as failure would re-offer forever."""
        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )
        stub.finalize_response_stream.return_value = None    # no contract

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        with patch.object(agent, "_process", AsyncMock(return_value="second")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0] == "second"

    async def test_identical_notice_is_not_repeated_within_the_cooldown(
        self, tmp_path, monkeypatch,
    ):
        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0].startswith(
            "PLUGIN-DEGRADED",
        )

        with patch.object(agent, "_process", AsyncMock(return_value="second")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0] == "second"

    async def test_notice_survives_agent_reconstruction(
        self, tmp_path, monkeypatch,
    ):
        """#551's actual complaint: a plugin mutation reconstructs the Agent
        (reload._construct_agent), which re-armed the old per-instance flag and
        re-showed the identical line three or four times in one setup flow.
        Suppression must outlive the instance."""
        agent, stub = self._agent_with_notice(
            tmp_path, monkeypatch, "PLUGIN-DEGRADED x",
        )
        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("telegram", "123", "hi"))
        assert stub.finalize_response_stream.call_args[0][0].startswith(
            "PLUGIN-DEGRADED",
        )

        fresh = _make_agent(tmp_path, role="assistant")
        stub2 = _StubTelegramChannel()
        fresh._channel_manager.register(stub2)
        with patch.object(fresh, "_process", AsyncMock(return_value="after")):
            await fresh.handle_message(_msg("telegram", "123", "hi"))
        assert stub2.finalize_response_stream.call_args[0][0] == "after"

    async def test_healthy_turn_sends_no_notice(
        self, tmp_path, monkeypatch,
    ):
        agent, stub = self._agent_with_notice(tmp_path, monkeypatch, None)

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("telegram", "123", "hi"))

        assert stub.finalize_response_stream.call_args[0][0] == "hello"

    async def test_no_final_text_channel_does_not_consume_the_notice(
        self, tmp_path, monkeypatch,
    ):
        """Sol/Terra diff r1: voice's `send()` is a documented no-op — its
        final text is never delivered out-of-band, so a channel declaring
        `delivers_final_text = False` must neither receive the notice nor
        burn its cooldown; a text channel still shows it afterwards."""
        import plugin_health
        plugin_health._notice_memo.clear()
        monkeypatch.setattr(
            plugin_health, "render_notice",
            lambda role, path=plugin_health.HEALTH_PATH: "PLUGIN-DEGRADED x",
        )
        agent = _make_agent(tmp_path, role="assistant")

        class _StubVoiceChannel:
            name = "voice"
            delivers_final_text = False

            def __init__(self):
                self.send = AsyncMock()

        stub = _StubVoiceChannel()
        agent._channel_manager.register(stub)

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(_msg("voice", "scope-1", "hi"))

        # Final text still flows (voice relays via the returned RESPONSE), but
        # unprefixed — and the cooldown is untouched, so a text channel still
        # gets the notice.
        assert stub.send.call_args[0][0] == "hello"

        text_agent = _make_agent(tmp_path, role="assistant")
        text_stub = _StubTelegramChannel()
        text_agent._channel_manager.register(text_stub)
        with patch.object(
            text_agent, "_process", AsyncMock(return_value="hi there"),
        ):
            await text_agent.handle_message(_msg("telegram", "123", "hi"))
        assert text_stub.finalize_response_stream.call_args[0][0].startswith(
            "PLUGIN-DEGRADED",
        )

    async def test_real_voice_channel_declares_no_final_text(self):
        """The gate reads `delivers_final_text` off the channel — pin that the
        REAL VoiceChannel declares it, not just the test stub."""
        from channels.voice.channel import VoiceChannel

        assert VoiceChannel.delivers_final_text is False


async def test_voice_channel_uses_voice_speaker_peer(tmp_path):
    # §4.3: the per-turn read no longer threads a user_peer (no ensure_session
    # / per-turn add_turn). The voice-speaker peer is carried into save_session
    # at session end instead. On the read side, a fresh voice turn pushes NO
    # overlay (blocked by clearance — voice=friends, overlay is private-only)
    # and NEVER auto-recalls (voice keeps the multi-strategy recall off the
    # first-utterance critical path).
    sem = FakeSemanticMemory(overlay="OVERLAY")
    agent = _make_agent(tmp_path, role="butler", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_msg("voice", "lr", "lights on"))

    assert len(sem.profile_calls) == 0   # voice clearance < private → overlay blocked
    assert sem.recall_calls == []        # voice never auto-recalls
    entry = agent._session_registry.get(
        build_scoped_session_key("voice", "butler", "lr"),
    )
    assert entry is not None


async def test_telegram_channel_autorecalls_on_fresh_session(tmp_path):
    # §4.3: the user_peer (nicola) is a save-path concern now (carried into
    # save_session), no longer threaded through the per-turn read. The
    # read-path contract for a fresh TEXT channel is: push the overlay AND
    # auto-recall the opening utterance against the role's bank.
    sem = FakeSemanticMemory(overlay="O", facts="F")
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert len(sem.profile_calls) == 1          # overlay pushed
    assert len(sem.recall_calls) == 1           # telegram auto-recalls
    assert sem.recall_calls[0]["query"] == "hi"


# ---------------------------------------------------------------------------
# Phase 5 / E-14: peer-overlay assembly (spec § 2.7, § 2.9)
# ---------------------------------------------------------------------------


async def test_fresh_text_turn_pushes_overlay_and_recalls(tmp_path):
    """Spec §4.3: a fresh TEXT turn pushes the mental-model overlay
    (profile) AND runs one query-specific recall over the readable scopes.
    Supersedes the old "1 overlay + N per-scope reads under gather" shape —
    the per-scope fan-out is gone; recall is a single tagged call."""
    sem = FakeSemanticMemory(overlay="overlay-content", facts="recall-content")
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    # One overlay (profile) call per fresh turn, keyed to the shared casa bank.
    assert sem.profile_calls == ["casa"]
    # One recall call: query == utterance, tags == sensitivity tiers readable at
    # telegram clearance (private → all four tiers), mid budget.
    assert len(sem.recall_calls) == 1
    call = sem.recall_calls[0]
    assert call["bank"] == "casa"
    assert call["query"] == "hi"
    assert call["tags"] == ["public", "friends", "family", "private"]
    assert call["budget"] == "mid"


async def test_overlay_block_present_when_overlay_non_empty(tmp_path):
    """Assembled memory_blocks contains <peer_overlay> when the profile
    overlay digest is non-empty (spec §4.3)."""
    sem = FakeSemanticMemory(overlay="OVERLAY_TEXT")
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

    captured: dict[str, str] = {}

    class _CapturingClient(FakeClient):
        def __init__(self, options):
            super().__init__(options)
            captured["system"] = options.system_prompt

    with patch("sdk_client_pool._default_make_client", _CapturingClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert "<peer_overlay>" in captured["system"]
    assert "OVERLAY_TEXT" in captured["system"]


async def test_overlay_failure_does_not_poison_recall(tmp_path, caplog):
    """profile() raising → overlay omitted, but the recall still proceeds
    and lands in the prompt (spec §4.3 — the two reads are independent)."""
    import logging

    class FailingProfileMemory(FakeSemanticMemory):
        async def profile(self, bank):
            self.profile_calls.append(bank)
            raise RuntimeError("simulated failure")

    sem = FailingProfileMemory(facts="recall-still-works")
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

    captured: dict[str, str] = {}

    class _CapturingClient(FakeClient):
        def __init__(self, options):
            super().__init__(options)
            captured["system"] = options.system_prompt

    with caplog.at_level(logging.WARNING, logger="agent"):
        with patch("sdk_client_pool._default_make_client", _CapturingClient):
            await agent._process(_msg("telegram", "123", "hi"))

    assert "<peer_overlay>" not in captured["system"]   # overlay omitted
    assert "<memory_context>" in captured["system"]     # recall present
    assert "recall-still-works" in captured["system"]
    assert any(
        "profile overlay failed" in r.message for r in caplog.records
    )


# NOTE (§4.3 read-path rewire): test_peer_overlay_empty_logs_info_line was
# dropped here. It asserted the legacy `peer_overlay_empty` INFO line emitted
# by the old MemoryProvider.peer_overlay_context read path. The SemanticMemory
# seam's `profile()` overlay has no empty-digest observability line (it would
# need to be designed + added deliberately, not inferred), so this assertion
# has no equivalent on the new contract. No replacement is invented.


async def test_system_prompt_contains_channel_context(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    prompt = FakeClient.captured_options.system_prompt
    assert "<channel_context>" in prompt
    assert "channel: telegram" in prompt
    assert "trust: authenticated (operator)" in prompt
    assert "</channel_context>" in prompt


async def test_system_prompt_memory_context_only_when_nonempty(tmp_path):
    # §4.3: <memory_context> now wraps the single recall digest (no per-scope
    # scope= attribute). Empty recall → no block; non-empty → block + content.
    sem = FakeSemanticMemory(facts="")
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))
    assert "<memory_context>" not in FakeClient.captured_options.system_prompt

    # Force a FRESH session so recall fires (the first turn above persisted a
    # registry entry into the shared sessions.json, which would otherwise
    # resume and skip the recall under §4.3).
    sem2 = FakeSemanticMemory(facts="## Recent\n[nicola] hi")
    agent2 = _make_agent(tmp_path, role="assistant", semantic_memory=sem2)
    # The pool captures ``decide=_resume_decision`` by reference at
    # construction (Agent.__init__), so patching the module-level
    # ``agent._resume_decision`` no longer reaches the pooled turn — patch the
    # live pool decision hook instead to force a fresh session (assertions
    # unchanged; §4.3 recall fires only on is_fresh).
    with patch("sdk_client_pool._default_make_client", FakeClient), \
         patch.object(agent2._pool, "_decide", return_value=ResumeDecision("new", None, False, None, "missing")):
        await agent2._process(_msg("telegram", "123", "hi"))
    prompt2 = FakeClient.captured_options.system_prompt
    assert "<memory_context>" in prompt2
    assert "scope=" not in prompt2          # per-scope attribute is gone
    assert "[nicola] hi" in prompt2


async def test_auto_recall_injects_attributed_memory_context(tmp_path):
    """Task 11 (Step 9): the pre-turn auto-recall now injects an ATTRIBUTED
    <memory_context> — each hit rendered with its recorded source — instead of
    a bare digest string. A telegram turn reads at private clearance, so the
    resident source (role + persona) is spoken; raw provenance/tier tags never
    escape into the prompt."""
    from personality_types import SpeakerProvenance

    prov = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler",
        persona_id="casa.personas/tina", persona_version="1.0.0",
        display_name="Tina", binding_digest="sha256:" + "5" * 64,
    )
    sem = FakeSemanticMemory(facts="the thermostat is set to 20C", provenance=prov)
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient), \
         patch.object(agent._pool, "_decide",
                      return_value=ResumeDecision("new", None, False, None, "missing")):
        await agent._process(_msg("telegram", "123", "hi"))
    prompt = FakeClient.captured_options.system_prompt
    assert "<memory_context>" in prompt
    assert "Tina previously said: the thermostat is set to 20C" in prompt
    assert "[source: resident:butler" in prompt
    assert "casa-source-" not in prompt   # reserved tag never escapes
    assert "sha256:" not in prompt        # binding digest never escapes


async def test_memory_failure_does_not_break_response(tmp_path, caplog):
    """Pins INV-TURN-004. Red case demonstrated: re-raising from _build_options' broad recall-failure handler fails this test."""
    import logging

    class BrokenSemanticMemory(FakeSemanticMemory):
        async def recall_items(self, *a, **kw):
            raise RuntimeError("hindsight down")

    sem = BrokenSemanticMemory()
    agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        with caplog.at_level(logging.WARNING):
            out = await agent._process(_msg("telegram", "123", "hi"))
    assert out == "pong"
    # v0.99.0 three-outcome contract: the failure logs as a distinguishable
    # "unavailable" outcome (not the old undifferentiated "recall failed").
    assert any(
        "auto_recall" in r.message and "outcome=unavailable" in r.message
        for r in caplog.records
    )
    prompt = FakeClient.captured_options.system_prompt
    assert "<memory_context>" not in prompt
    assert "<channel_context>" in prompt


# NOTE (Task 7, spec §4.2): the per-turn `add_turn` write and its background
# task wrapper (`_add_turn_bg`) were retired in favour of session-granularity
# saves. Two tests that exercised that removed machinery were dropped here:
#   - test_add_turn_failure_logs_warning: asserted the now-deleted
#     `_add_turn_bg` try/except logged a warning when add_turn raised. The
#     agent no longer calls add_turn per turn, so the path is gone.
#   - test_agent_retains_add_turn_task_strong_reference: asserted the
#     background add_turn task was strongly referenced in `_bg_tasks`. That
#     machinery is gone; session-granularity saves are handled by the reaper.
# The per-turn write_scope classification was subsequently removed in Task 6
# (tier model); tiering now lives in the freshness reaper off the critical path.


class TestRetryIntegration:
    async def test_transient_sdk_error_retries_then_succeeds(
        self, tmp_path, caplog,
    ):
        FakeClient.reset()
        # Dynamic subclass so type name contains "CLI" → SDK_ERROR class.
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        exc = CLIConnectionError("upstream reset")
        # Fail first attempt; succeed second.
        FakeClient.failure_schedule = [exc, None]

        agent = _make_agent(tmp_path, role="assistant")
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            text = await agent._process(_msg("telegram", "123", "hi"))

        assert text == "pong"
        assert FakeClient.attempts == 2

    async def test_one_rate_limit_then_success(self, tmp_path):
        FakeClient.reset()
        FakeClient.failure_schedule = [
            RuntimeError("429 rate limit"),
            None,
        ]
        agent = _make_agent(tmp_path, role="assistant")
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            text = await agent._process(_msg("telegram", "123", "hi"))
        assert text == "pong"
        assert FakeClient.attempts == 2

    async def test_unknown_exception_does_not_retry(self, tmp_path):
        FakeClient.reset()
        FakeClient.failure_schedule = [ValueError("bad input")]
        agent = _make_agent(tmp_path, role="assistant")
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            with pytest.raises(ValueError):
                await agent._process(_msg("telegram", "123", "hi"))
        assert FakeClient.attempts == 1

    async def test_cancellation_propagates_without_retry(self, tmp_path):
        FakeClient.reset()
        FakeClient.failure_schedule = [asyncio.CancelledError()]
        agent = _make_agent(tmp_path, role="assistant")
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep() as sleep:
            with pytest.raises(asyncio.CancelledError):
                await agent._process(_msg("telegram", "123", "hi"))
        assert FakeClient.attempts == 1
        sleep.assert_not_awaited()

    async def test_on_token_replays_final_text_after_retry(self, tmp_path):
        """Spec §3.2: first attempt's tokens are discarded; on_token
        sees the cumulative final text from the successful attempt."""
        FakeClient.reset()
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        exc = CLIConnectionError("upstream reset")
        FakeClient.failure_schedule = [exc, None]

        agent = _make_agent(tmp_path, role="butler")

        seen_tokens: list[str] = []
        async def on_token(txt: str) -> None:
            seen_tokens.append(txt)

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            msg = _msg("voice", "lr", "status?")
            await agent._process(msg, on_token=on_token)

        # The final attempt emitted "pong" once via on_token. Because
        # our FakeClient raises during query() before streaming, no
        # partial tokens leaked from the failed attempt. The caller
        # sees the cumulative final text at least at the end.
        assert seen_tokens[-1] == "pong"
        assert FakeClient.attempts == 2


class TestAssistantMessageSeparator:
    """E-2: Ellen's cumulative attempt_text must insert \\n\\n between
    successive AssistantMessages, so the streamed message reads as discrete
    thoughts rather than one glued paragraph
    (bug-review-2026-04-29-exploration.md § E-2)."""

    async def test_two_assistant_messages_get_double_newline_separator(
        self, tmp_path,
    ):
        """Feed SDK with ack + final answer as two AssistantMessages; the
        last on_token argument must contain '\\n\\n' between them."""

        class _TwoMsgClient(FakeClient):
            async def receive_response(self):
                if self._scheduled is not None:
                    raise self._scheduled
                yield _mk_assistant("Let me ask Tina to pull the house state for you.")
                yield _mk_assistant("Here's the snapshot: lights are off.")
                yield _mk_result("sdk-sid-e2", usage=FakeClient.usage)

        FakeClient.reset()
        agent = _make_agent(tmp_path, role="assistant")

        seen: list[str] = []

        async def on_token(txt: str) -> None:
            seen.append(txt)

        with patch("sdk_client_pool._default_make_client", _TwoMsgClient):
            msg = _msg("telegram", "123", "are the lights on?")
            await agent._process(msg, on_token=on_token)

        # on_token was called at least twice (once per AssistantMessage)
        assert len(seen) >= 2, f"expected ≥2 on_token calls, got {len(seen)}: {seen}"
        last = seen[-1]
        # Both pieces present in the cumulative final
        assert "Let me ask Tina" in last
        assert "Here's the snapshot" in last
        # The bug: pre-fix the cumulative reads "...for you.Here's..."
        assert "for you.Here's" not in last, (
            f"E-2 not fixed: ack and answer concatenated without separator. "
            f"Final cumulative on_token text: {last!r}"
        )
        # Positive: \n\n between the two AssistantMessages
        assert "for you.\n\nHere's the snapshot" in last, (
            f"Expected '\\n\\n' between successive AssistantMessages. Got: {last!r}"
        )


class TestPhase4bDispatch:
    """Phase 4b Bug 3 parity: Ellen's _attempt_sdk_turn dispatches
    every SDK message kind through sdk_logging, identical shape to
    the in_casa-driver path."""

    async def test_attempt_sdk_turn_logs_per_message(
        self, tmp_path, caplog,
    ):
        import logging
        from claude_agent_sdk import (
            AssistantMessage as _AM, ResultMessage as _RM,
            SystemMessage as _SM, TextBlock as _TB,
        )

        class _RichFakeClient:
            captured_options = None
            def __init__(self, options):
                _RichFakeClient.captured_options = options

            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, text): pass

            async def receive_response(self):
                init = _SM.__new__(_SM)
                init.subtype = "init"  # type: ignore[attr-defined]
                init.data = {"model": "sonnet", "session_id": "abcdef12-fff"}  # type: ignore[attr-defined]
                yield init
                yield _mk_assistant("Hi.")
                rm = _RM.__new__(_RM)
                rm.session_id = "abcdef12-fff"  # type: ignore[attr-defined]
                rm.usage = {"input_tokens": 50, "output_tokens": 5}  # type: ignore[attr-defined]
                rm.num_turns = 1  # type: ignore[attr-defined]
                rm.total_cost_usd = 0.0001  # type: ignore[attr-defined]
                yield rm

        agent = _make_agent(tmp_path, role="assistant")

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            with patch("sdk_client_pool._default_make_client", _RichFakeClient):
                await agent._process(_msg("telegram", "200", "hi"))

        msgs = [r.getMessage() for r in caplog.records if r.name == "sdk"]
        assert any("system_init" in m and "model=sonnet" in m for m in msgs), msgs
        assert any("assistant_message idx=1" in m and "chars=3" in m for m in msgs), msgs
        assert any("turn_done" in m and "turns=1" in m for m in msgs), msgs

    async def test_attempt_sdk_turn_injects_stderr_callback(
        self, tmp_path,
    ):
        """Bug 4: ClaudeAgentOptions passed to ClaudeSDKClient must
        carry our stderr callback so the SDK pipes the CLI subprocess
        stderr through subprocess_cli.connect()."""
        FakeClient.reset()
        agent = _make_agent(tmp_path, role="assistant")

        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_msg("telegram", "201", "hi"))

        opts = FakeClient.captured_options
        assert opts is not None, "FakeClient never received options"
        assert callable(opts.stderr), (
            "Phase 4b Bug 4: ClaudeAgentOptions.stderr must be set "
            "to make_stderr_logger's callback before ClaudeSDKClient is built"
        )

    async def test_on_message_guard_stops_second_validation_result(
        self, tmp_path,
    ):
        agent = _make_agent(tmp_path, role="butler")
        on_message, _state = agent._make_on_message(
            None, VoiceTurnGuard.ha_direct(),
        )

        await on_message(_mk_tool_use(
            "tool-1", "mcp__homeassistant__GetLiveContext",
            {"domain": "light"},
        ))
        await on_message(_mk_tool_result(
            "tool-1", is_error=True, text="InputValidationError",
        ))
        await on_message(_mk_tool_use(
            "tool-2", "mcp__homeassistant__GetLiveContext",
        ))
        with pytest.raises(
            VoiceToolLoopError, match="validation_correction_exhausted",
        ):
            await on_message(_mk_tool_result(
                "tool-2", is_error=True, text="invalid tool input",
            ))


class TestVoiceTurnGuardWiring:
    @pytest.mark.parametrize(
        ("channel", "policy", "should_raise"),
        [
            ("voice", "ha_direct", True),
            ("voice", "none", False),
            ("telegram", "ha_direct", False),
        ],
    )
    async def test_guard_is_enabled_only_for_ha_direct_voice_turns(
        self, tmp_path, channel, policy, should_raise,
    ):
        agent = _make_agent(tmp_path, role="butler")
        agent.config.tools.voice_guard = policy
        ScriptedToolClient.reset([
            _mk_tool_use(
                "tool-1", "mcp__homeassistant__GetLiveContext",
                {"domain": "light"},
            ),
            _mk_tool_result(
                "tool-1", is_error=True, text="InputValidationError",
            ),
            _mk_tool_use(
                "tool-2", "mcp__homeassistant__GetLiveContext",
            ),
            _mk_tool_result(
                "tool-2", is_error=True, text="schema validation failed",
            ),
            _mk_result("guard-sid"),
        ])

        with patch(
            "sdk_client_pool._default_make_client", ScriptedToolClient,
        ):
            if should_raise:
                with pytest.raises(
                    VoiceToolLoopError,
                    match="validation_correction_exhausted",
                ):
                    await agent._process(_msg(channel, "guard-scope"))
                assert all(
                    client.disconnected
                    for client in ScriptedToolClient.instances
                )
                assert agent._pool.stats()["entries"] == 0
            else:
                await agent._process(_msg(channel, "guard-scope"))

    @pytest.mark.parametrize("pool_mode", ["on", "off"])
    async def test_voice_guard_is_fresh_for_each_retry_attempt(
        self, tmp_path, monkeypatch, pool_mode,
    ):
        monkeypatch.setenv("SDK_CLIENT_POOL", pool_mode)
        agent = _make_agent(tmp_path, role="butler")
        agent.config.tools.voice_guard = "ha_direct"
        RetryableError = type("CLIConnectionError", (RuntimeError,), {})
        ScriptedToolClient.reset(
            [
                _mk_tool_use(
                    "attempt-1-tool",
                    "mcp__homeassistant__HassTurnOff",
                    {"name": "office"},
                ),
                _mk_tool_result(
                    "attempt-1-tool", is_error=True,
                    text="input validation failed",
                ),
                RetryableError("upstream reset"),
            ],
            [
                _mk_tool_use(
                    "attempt-2-tool",
                    "mcp__homeassistant__HassTurnOff",
                    {"name": "office lamp"},
                ),
                _mk_tool_result(
                    "attempt-2-tool", is_error=True,
                    text="invalid tool input",
                ),
                _mk_assistant("Recovered cleanly."),
                _mk_result("retry-guard-sid"),
            ],
        )

        with patch(
            "sdk_client_pool._default_make_client", ScriptedToolClient,
        ), patch_retry_sleep():
            text = await agent._process(_msg(
                "voice", f"retry-guard-{pool_mode}",
            ))

        assert text == "Recovered cleanly."


# ---------------------------------------------------------------------------
# Correlation-id end-to-end (spec 5.2 §7.4)
# ---------------------------------------------------------------------------


class TestCorrelationId:
    """End-to-end dispatch → every log record during the turn carries
    the cid from msg.context; two concurrent turns are distinguishable."""

    @pytest.fixture(autouse=True)
    def _cid_factory(self):
        """Install a minimal LogRecord factory that tags every new
        record with ``record.cid = cid_var.get()``. This mirrors what
        ``install_logging`` does in production but skips the
        StreamHandler (so test stdout stays clean). Needed because
        Python's ``Logger.callHandlers`` walks ancestor HANDLERS but
        NOT ancestor LOGGERS' filter chains — a filter on root would
        not fire for records from descendant loggers. The factory
        runs inside ``Logger.makeRecord``, so every record everywhere
        in the process gets tagged regardless of emission path.
        Scoped to this class via autouse; restored on teardown."""
        import logging
        from log_cid import cid_var
        orig_factory = logging.getLogRecordFactory()
        def _factory(*args, **kwargs):
            record = orig_factory(*args, **kwargs)
            record.cid = cid_var.get()
            return record
        logging.setLogRecordFactory(_factory)
        try:
            yield
        finally:
            logging.setLogRecordFactory(orig_factory)

    async def test_single_turn_records_share_cid(
        self, tmp_path, caplog,
    ):
        import logging
        from bus import BusMessage, MessageBus, MessageType

        FakeClient.reset()
        FakeClient.response_text = "pong"
        agent = _make_agent(tmp_path, role="assistant")

        bus = MessageBus()
        # #418 (2nd load-flake in this test): scope the assertion to records
        # the turn OWNS. Module-name filtering could not tell the turn's own
        # ``agent``/``retry``/``bus`` records from a background task's (pool
        # sweeper, cold retain, ...) emitted with no cid inside the window —
        # under xdist load such interleavings are likely. Capture the dispatch
        # task's name from INSIDE the handler (the bus awaits the handler in
        # its dispatch task, so bus/agent/retry records of this turn all carry
        # it as ``taskName``) and filter on it: ownership by construction, not
        # by timing.
        turn_task_names: set[str | None] = set()

        async def capturing_handle(m):
            turn_task_names.add(asyncio.current_task().get_name())
            return await agent.handle_message(m)

        bus.register("assistant", capturing_handle)

        caplog.set_level(logging.INFO)

        msg = BusMessage(
            type=MessageType.REQUEST,
            source="test", target="assistant",
            content="hi", channel="telegram",
            context={"chat_id": "42", "cid": "abcd1234"},
        )

        # Baseline BEFORE the dispatch. `caplog.records` accumulates from the
        # start of the test, so a plain snapshot also folds in CONSTRUCTION
        # records — `Agent.__init__` logs `agent_capabilities` at INFO, with no
        # cid bound because building an agent is not part of any turn.
        #
        # Whether that record is captured depends on the `agent` logger's level
        # when `_make_agent` ran, which a test that executed EARLIER ON THE SAME
        # xdist worker can leave at INFO. So the failure is invisible until file
        # scheduling changes — `--dist loadfile` re-assigns files whenever the
        # test set does — and then it is deterministic, not flaky: v0.163.0
        # added test files and this went red on CI twice in a row while passing
        # locally every time.
        #
        # Bounding the window at BOTH ends is what makes the assertion mean what
        # it says. The tail matters for the same reason and was fixed first: the
        # `finally` below cancels the agent loop, and on a loaded runner that
        # logs before the assertion runs.
        before_dispatch = len(caplog.records)
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            loop = asyncio.create_task(bus.run_agent_loop("assistant"))
            try:
                result = await bus.request(msg, timeout=5)
                during_dispatch = caplog.records[before_dispatch:]
            finally:
                loop.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loop

        assert "pong" in str(result.content)

        # Every record emitted during the dispatch window BY THE TURN'S OWN
        # TASK must carry the cid. Scoped to Casa modules (avoid pytest's own
        # handler records) AND to the dispatch task (see capturing_handle
        # above) — records other tasks emit in the window are not this test's
        # to judge.
        relevant = [
            r for r in during_dispatch
            if r.name in {"agent", "retry", "bus"}
            and getattr(r, "taskName", None) in turn_task_names
        ]
        # At least one record should have been emitted — the agent log
        # line "SDK session for 'assistant': sess-..." is unconditional
        # post-retry. Tolerate zero if the agent stays silent in this
        # fake path; the point is: none of them may have cid="-".
        cids_seen = {getattr(r, "cid", None) for r in relevant}
        # Strip any records with cid="-" that slipped in before the
        # first dispatch (there should be none, but be explicit).
        cids_seen.discard(None)
        # No stray cid="-" during the turn.
        assert cids_seen <= {"abcd1234"}, cids_seen

    async def test_two_concurrent_turns_produce_distinct_cids(
        self, tmp_path, caplog,
    ):
        import logging
        from bus import BusMessage, MessageBus, MessageType

        FakeClient.reset()
        FakeClient.response_text = "pong"
        agent = _make_agent(tmp_path, role="assistant")

        bus = MessageBus()
        bus.register("assistant", agent.handle_message)

        caplog.set_level(logging.INFO)
        before_dispatch = len(caplog.records)

        def _mk(cid: str, chat_id: str) -> BusMessage:
            return BusMessage(
                type=MessageType.REQUEST,
                source="test", target="assistant",
                content="hi", channel="telegram",
                context={"chat_id": chat_id, "cid": cid},
            )

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            loop = asyncio.create_task(bus.run_agent_loop("assistant"))
            try:
                # Fire both concurrently.
                results = await asyncio.gather(
                    bus.request(_mk("aaaa1111", "A"), timeout=5),
                    bus.request(_mk("bbbb2222", "B"), timeout=5),
                )
            finally:
                loop.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loop

        assert len(results) == 2

        # Every agent/retry/bus record must carry one of the two cids;
        # neither may cross-contaminate (dispatcher scopes cid per task).
        # Sliced from the pre-dispatch baseline for the same reason as the
        # sibling above — this one already discards cid="-" records, so
        # construction cannot fail it, but reading the whole log to judge one
        # window is the shape that went wrong and it should not survive here.
        relevant = [
            r for r in caplog.records[before_dispatch:]
            if r.name in {"agent", "retry", "bus"}
            and getattr(r, "cid", "-") != "-"
        ]
        cids_seen = {r.cid for r in relevant}
        assert cids_seen <= {"aaaa1111", "bbbb2222"}
        # Both turns emitted at least one cid-tagged record.
        assert "aaaa1111" in cids_seen
        assert "bbbb2222" in cids_seen


# ---------------------------------------------------------------------------
# Token budget monitoring (spec 5.2 §5)
# ---------------------------------------------------------------------------


class TestTokenBudgetMonitoring:
    """End-to-end: Agent._process drives the BudgetTracker after
    get_context and emits the per-turn summary log line after the SDK
    call returns."""

    async def test_memory_recorder_called_on_each_successful_turn(
        self, tmp_path,
    ):
        """The recorder should see one call per turn with the digest
        size estimated from the assembled memory_blocks (spec §4.3:
        the recall facts wrapped in <memory_context>)."""
        from personality_types import RecallHit, SpeakerProvenance
        from recall_renderer import render_recall
        from tokens import estimate_tokens
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        # Task 11: the recorded block is now the ATTRIBUTED render of the recall
        # hits (bounded by token_budget), not the raw facts string. Keep the
        # fact small enough to render fully so the expected size is exact.
        facts = "x" * 100
        # Overlay empty so the recorded block is exactly the recall context.
        sem = FakeSemanticMemory(overlay="", facts=facts)
        agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

        observed: list[tuple[str, int, int]] = []
        original_record = agent._budget_tracker.record
        def _spy(session_id, used_tokens, budget):
            observed.append((session_id, used_tokens, budget))
            original_record(session_id, used_tokens, budget)
        agent._budget_tracker.record = _spy  # type: ignore[method-assign]

        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_msg("telegram", "123", "hi"))

        digest = render_recall(
            (RecallHit(
                text=facts, memory_type="world", sensitivity="friends",
                application_tags=(), provenance=None, backend_id="b1",
                document_id=None, chunk_id=None, source_fact_ids=None,
                metadata=None, context=None, score=None,
            ),),
            current_speaker=SpeakerProvenance(speaker_kind="system"),
            surface="text", clearance="private", token_budget=1000,
        )
        # #581: the block now opens with the memory-use instruction line, and
        # that line is inside the budget accounting exactly as the digest is.
        from recall_renderer import READABLE_SLICE_PROMPT_LINE
        expected = estimate_tokens(
            f"<memory_context>\n{READABLE_SLICE_PROMPT_LINE}\n{digest}\n"
            f"</memory_context>"
        )
        expected_channel_key = build_scoped_session_key("telegram", "assistant", "123")
        assert observed == [(f"{expected_channel_key}-assistant", expected, 1000)]

    async def test_three_consecutive_overruns_emit_one_warning(
        self, tmp_path, caplog,
    ):
        """Spec §5.2: warn after three consecutive overruns; suppress
        thereafter for that session."""
        import logging as _logging
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        # 5000-token estimate vs 1000 budget → overrun.
        # §4.3: the oversized memory_block only fires on a FRESH session, so
        # force every turn fresh — the budget streak is keyed on
        # channel_key+role, stable across these five fresh turns. Task 11: the
        # RECALL digest is now bounded by token_budget, so the oversized block
        # is driven through the (un-capped) mental-model overlay instead — the
        # budget-overrun machinery under test is identical either way.
        sem = FakeSemanticMemory(overlay="x" * 20000, facts="")
        agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

        caplog.set_level(_logging.WARNING, logger="tokens")
        # Force every turn fresh via the live pool decision hook: the pool
        # captured ``decide`` by reference at construction, so patching the
        # module-level ``agent._resume_decision`` would not reach it.
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch.object(agent._pool, "_decide", return_value=ResumeDecision("new", None, False, None, "missing")):
            for _ in range(5):
                await agent._process(_msg("telegram", "123", "hi"))

        expected_channel_key = build_scoped_session_key("telegram", "assistant", "123")
        rows = [
            r for r in caplog.records
            if r.name == "tokens" and f"{expected_channel_key}-assistant" in r.getMessage()
        ]
        assert len(rows) == 1, [r.getMessage() for r in rows]

    async def test_retried_turn_records_budget_once(self, tmp_path):
        """#349: retry_sdk_call re-invokes _build_options per SDK attempt; the
        budget streak must advance once per LOGICAL turn, not per attempt."""
        FakeClient.reset()
        FakeClient.failure_schedule = [asyncio.TimeoutError(), None]
        sem = FakeSemanticMemory(overlay="x" * 20000, facts="")
        agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

        observed: list[tuple[str, int, int]] = []
        agent._budget_tracker.record = (  # type: ignore[method-assign]
            lambda *a: observed.append(a)
        )

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            await agent._process(_msg("telegram", "123", "hi"))

        assert FakeClient.attempts == 2
        assert len(observed) == 1

    async def test_failed_turn_records_no_budget(self, tmp_path):
        """A turn that never succeeds must not advance (or reset) the streak."""
        FakeClient.reset()
        FakeClient.failure_schedule = [RuntimeError("non-retryable")]
        sem = FakeSemanticMemory(overlay="x" * 20000, facts="")
        agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

        observed: list[tuple[str, int, int]] = []
        agent._budget_tracker.record = (  # type: ignore[method-assign]
            lambda *a: observed.append(a)
        )

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            with pytest.raises(RuntimeError):
                await agent._process(_msg("telegram", "123", "hi"))

        assert observed == []

    async def test_stale_stash_dropped_when_final_attempt_has_no_block(
        self, tmp_path,
    ):
        """Terra r1 S2: attempt 1 assembles an oversized block and fails;
        attempt 2 assembles NO memory block and succeeds. The successful turn
        carried no budgeted context, so nothing may be recorded."""

        class _SequenceOverlayMemory(FakeSemanticMemory):
            def __init__(self, overlays):
                super().__init__(overlay="", facts="")
                self._overlays = list(overlays)

            async def profile(self, bank):
                self.profile_calls.append(bank)
                return self._overlays.pop(0) if self._overlays else ""

        FakeClient.reset()
        FakeClient.failure_schedule = [asyncio.TimeoutError(), None]
        sem = _SequenceOverlayMemory(["x" * 20000, ""])
        agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

        observed: list[tuple[str, int, int]] = []
        agent._budget_tracker.record = (  # type: ignore[method-assign]
            lambda *a: observed.append(a)
        )

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            await agent._process(_msg("telegram", "123", "hi"))

        assert FakeClient.attempts == 2
        assert observed == []

    async def test_budget_not_recorded_when_post_success_persist_fails(
        self, tmp_path,
    ):
        """Sol r1 S2: the record commits only when the TURN completes — an SDK
        success followed by a session-registry persistence failure is still a
        failed turn and must not advance the streak."""
        FakeClient.reset()
        sem = FakeSemanticMemory(overlay="x" * 20000, facts="")
        agent = _make_agent(tmp_path, role="assistant", semantic_memory=sem)

        observed: list[tuple[str, int, int]] = []
        agent._budget_tracker.record = (  # type: ignore[method-assign]
            lambda *a: observed.append(a)
        )

        async def _boom(*a, **kw):
            raise RuntimeError("persist failed")

        agent._session_registry.register = _boom  # type: ignore[method-assign]

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            with pytest.raises(RuntimeError):
                await agent._process(_msg("telegram", "123", "hi"))

        assert observed == []

    async def test_turn_tokens_log_carries_usage_fields(
        self, tmp_path, caplog,
    ):
        """One INFO-level turn_tokens line per successful _process call,
        with the usage fields the SDK reported. No cost field.
        (G-fix 2026-05-29: the agent-logger summary prefix is now
        ``turn_tokens``, disjoint from the sdk logger's ``turn_done``.)"""
        import logging as _logging
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 1203,
            "output_tokens": 82,
            "cache_read_input_tokens": 5021,
            "cache_creation_input_tokens": 3000,
        }
        agent = _make_agent(tmp_path, role="butler")

        caplog.set_level(_logging.INFO, logger="agent")
        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_msg("voice", "lr", "lights on"))

        # Phase 4b added an `sdk` logger turn_done line; this test asserts
        # the `agent` logger's per-turn summary specifically (now `turn_tokens`).
        rows = [
            r for r in caplog.records
            if r.name == "agent" and "turn_tokens" in r.getMessage()
        ]
        assert len(rows) == 1
        msg = rows[0].getMessage()
        assert "role=butler" in msg
        assert "channel=voice" in msg
        assert "input=1203" in msg
        assert "output=82" in msg
        assert "cache_read=5021" in msg
        assert "cache_write=3000" in msg
        # Explicit anti-assertion: no cost field under Max.
        assert "cost_est" not in msg
        assert "cost=" not in msg

    async def test_usage_resets_between_retry_attempts(
        self, tmp_path, caplog,
    ):
        """Spec §3 + §5: streaming accumulator resets per attempt; the
        turn_done line reflects only the final attempt's usage."""
        import logging as _logging
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 999,
            "output_tokens": 11,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.failure_schedule = [
            CLIConnectionError("upstream reset"), None,
        ]

        agent = _make_agent(tmp_path, role="assistant")

        caplog.set_level(_logging.INFO, logger="agent")
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            await agent._process(_msg("telegram", "123", "hi"))

        assert FakeClient.attempts == 2
        # Phase 4b added an `sdk` logger turn_done line; the `agent` logger
        # summary still fires once per overall _process call (now `turn_tokens`).
        rows = [
            r for r in caplog.records
            if r.name == "agent" and "turn_tokens" in r.getMessage()
        ]
        assert len(rows) == 1
        msg = rows[0].getMessage()
        # Only the second attempt's usage is in the line.
        assert "input=999" in msg
        assert "output=11" in msg


# ---------------------------------------------------------------------------
# TestResumeResilience — 5.8 §3.2
# ---------------------------------------------------------------------------


def _make_agent_with_registry(
    registry: SessionRegistry,
    role: str = "butler",
) -> Agent:
    """Like _make_agent, but takes a pre-constructed SessionRegistry so
    tests can pre-populate entries.
    """
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(
            token_budget=1000,
            read_strategy="per_turn",
        ),
        # Task 9: real resident identity triple (see _make_agent).
        role_id=resident_role_id(role),
        kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov(role),
    )
    return Agent(
        config=cfg,
        session_registry=registry,
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=FakeSemanticMemory(),
    )


class TestResumeResilience:
    """Agent._process recovers when claude CLI rejects a stale resume (5.8)."""

    async def test_stale_resume_cleared_and_retried_fresh(self, tmp_path):
        """First attempt resumes stale-sid -> ProcessError; fallback clears
        the stale id, retries with resume=None, succeeds with fresh sid."""
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register(
            build_scoped_session_key("voice", "butler", "probe-scope"),
            resident_role_id("butler"), "stale-sid-abc",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            text = await agent._process(_msg("voice", "probe-scope", "hi"))

        assert text == "pong"
        assert FakeClient.attempts == 2

    async def test_sdk_retry_fresh_emits_telemetry(self, tmp_path, caplog):
        """Bug 5: when the resume → ProcessError → fresh-retry path fires,
        one structured INFO line records the event with exit_code,
        prior_sid, and stderr_tail. Verifiable via caplog."""
        import logging
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError(
                "CLI exit",
                exit_code=1,
                stderr="stale-session-error\nat line 42",
            ),
            None,
        ]
        FakeClient.response_text = "ok after retry"

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register(
            build_scoped_session_key("telegram", "butler", "202"),
            resident_role_id("butler"), "STALE-SID-1",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        with caplog.at_level(logging.INFO, logger="agent"):
            with patch("sdk_client_pool._default_make_client", FakeClient), \
                 patch_retry_sleep():
                await agent._process(_msg("telegram", "202", "hi"))

        msgs = [r.getMessage() for r in caplog.records if r.name == "agent"]
        retry_lines = [m for m in msgs if "sdk_retry_fresh" in m]
        assert len(retry_lines) == 1, (
            f"expected exactly one sdk_retry_fresh INFO line; got: {retry_lines}"
        )
        line = retry_lines[0]
        assert "exit_code=1" in line, line
        assert "prior_sid=STALE-SID-1" in line, line
        # stderr_tail truncated at 200 chars; newlines escaped to \\n
        assert "stderr_tail=" in line, line
        assert "stale-session-error" in line, line
        # \n in stderr → \\n in tail
        assert "\\n" in line, line

    async def test_fallback_uses_resume_none_on_second_attempt(self, tmp_path):
        """The second FakeClient construction must see options.resume=None."""
        from claude_agent_sdk import ProcessError

        captured_resumes: list[str | None] = []

        class _CapturingFakeClient(FakeClient):
            def __init__(self, options):
                captured_resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register(
            build_scoped_session_key("voice", "butler", "probe-scope"),
            resident_role_id("butler"), "stale-sid-abc",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        with patch("sdk_client_pool._default_make_client", _CapturingFakeClient), \
             patch_retry_sleep():
            await agent._process(_msg("voice", "probe-scope", "hi"))

        assert len(captured_resumes) == 2
        assert captured_resumes[0] == "stale-sid-abc"
        assert captured_resumes[1] is None

    async def test_process_error_without_resume_reraises(self, tmp_path):
        """Fresh session (no registry entry) + ProcessError -> propagates."""
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        agent = _make_agent_with_registry(reg, role="butler")

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            with pytest.raises(ProcessError):
                await agent._process(_msg("voice", "fresh-scope", "hi"))

        assert FakeClient.attempts == 1
        assert reg.get(
            build_scoped_session_key("voice", "butler", "fresh-scope"),
        ) is None

    async def test_fallback_second_process_error_reraises(self, tmp_path):
        """Both attempts ProcessError -> second propagates; stale id cleared."""
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            ProcessError("Command failed with exit code 1", exit_code=1),
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register(
            build_scoped_session_key("voice", "butler", "probe-scope"),
            resident_role_id("butler"), "stale-sid-xyz",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            with pytest.raises(ProcessError):
                await agent._process(_msg("voice", "probe-scope", "hi"))

        assert FakeClient.attempts == 2
        entry = reg.get(build_scoped_session_key("voice", "butler", "probe-scope"))
        assert entry is not None
        assert "sdk_session_id" not in entry

    async def test_pool_unavailable_fallback_recovers_stale_resume(
        self, tmp_path,
    ):
        """#537: the pool raises PoolUnavailable AND the bypass fallback's
        resume attempt hits the stale-resume ProcessError. The recovery
        (conditional clear + retry fresh) must fire for the fallback exactly
        as it does for the primary attempt — the double fault must not
        surface raw nor leave the stale sid registered."""
        from claude_agent_sdk import ProcessError
        from sdk_client_pool import PoolUnavailable

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        key = build_scoped_session_key("voice", "butler", "probe-scope")
        await reg.register(
            key, resident_role_id("butler"), "stale-sid-537",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        with patch.object(
            agent._pool, "turn",
            AsyncMock(side_effect=PoolUnavailable("pool closing")),
        ), patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            text = await agent._process(_msg("voice", "probe-scope", "hi"))

        assert text == "pong"
        assert FakeClient.attempts == 2      # stale bypass + fresh retry
        entry = reg.get(key)
        assert entry is not None
        assert entry.get("sdk_session_id") != "stale-sid-537"

    async def test_recovery_clear_preserves_concurrent_registration(
        self, tmp_path,
    ):
        """#349: the stale-resume recovery must clear the registry entry only
        while it still carries the sid that FAILED. A concurrent same-key turn
        that registered a NEW session between the failure and the clear keeps
        its pointer, and the retry resumes that newer session instead of
        splitting the conversation with a fresh one."""
        from claude_agent_sdk import ProcessError

        captured_resumes: list[str | None] = []

        class _CapturingFakeClient(FakeClient):
            def __init__(self, options):
                captured_resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        key = build_scoped_session_key("voice", "butler", "probe-scope")
        await reg.register(
            key, resident_role_id("butler"), "stale-sid-abc",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        # Deterministic interleaving: a concurrent turn's register() lands
        # after the resume failure but before the recovery's clear runs.
        orig_clear = reg.clear_sdk_session

        async def _racy_clear(channel_key, *args, **kwargs):
            await reg.register(
                channel_key, resident_role_id("butler"), "sid-NEW",
                binding_digest=RESIDENT_DIGEST,
                speaker_provenance=resident_prov("butler"),
                user_provenance=STUB_USER_PROV,
            )
            await orig_clear(channel_key, *args, **kwargs)

        reg.clear_sdk_session = _racy_clear  # type: ignore[method-assign]

        with patch(
            "sdk_client_pool._default_make_client", _CapturingFakeClient,
        ), patch_retry_sleep():
            text = await agent._process(_msg("voice", "probe-scope", "hi"))

        assert text == "pong"
        # First attempt resumed the stale sid; the retry after the declined
        # clear resumes the CONCURRENT registration, not a fresh session.
        assert captured_resumes[0] == "stale-sid-abc"
        assert captured_resumes[1] == "sid-NEW"

    async def test_fallback_logs_warning(self, tmp_path, caplog):
        """A single WARNING with key + sid fires on fallback."""
        import logging as _logging
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        expected_channel_key = build_scoped_session_key("voice", "butler", "probe-scope")
        await reg.register(
            expected_channel_key, resident_role_id("butler"), "stale-sid-xyz",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )

        agent = _make_agent_with_registry(reg, role="butler")

        caplog.set_level(_logging.WARNING, logger="agent")
        with patch("sdk_client_pool._default_make_client", FakeClient), \
             patch_retry_sleep():
            await agent._process(_msg("voice", "probe-scope", "hi"))

        warning_records = [
            r for r in caplog.records
            if r.levelno == _logging.WARNING
            and "SDK resume failed" in r.getMessage()
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert expected_channel_key in msg
        assert "stale-sid-xyz" in msg


# ---------------------------------------------------------------------------
# TestOriginVar — Phase 3.1 §6.6
# ---------------------------------------------------------------------------


class TestOriginVar:
    """`origin_var` is set for the duration of a turn and reset on exit.
    Nested asyncio tasks (spawned from a tool handler) inherit the value
    via asyncio's contextvars snapshot."""

    async def test_origin_var_set_during_turn(self, tmp_path):
        """FakeClient captures origin_var.get(None) inside receive_response
        (which runs while the turn is in-flight). After the turn returns
        we expect origin_var to be reset to None (default sentinel)."""
        import agent as agent_mod

        captured: dict[str, Any] = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["inside_turn"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        a = _make_agent(tmp_path, role="assistant")
        msg = _msg("telegram", "777", "hello")
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)

        assert captured["inside_turn"] is not None
        origin = captured["inside_turn"]
        assert origin["role"] == "assistant"
        assert origin["channel"] == "telegram"
        assert origin["chat_id"] == "777"
        assert origin["user_text"] == "hello"

        # After the turn, ContextVar is back to None.
        assert agent_mod.origin_var.get(None) is None

    async def test_origin_snapshot_carries_provenance_fields(self, tmp_path):
        """A:§1: the origin snapshot must carry message_type/source (for
        turn_provenance's transport classification) and execution_role,
        which starts equal to `role` for a direct (non-delegated) turn."""
        import agent as agent_mod
        from bus import MessageType

        captured: dict[str, Any] = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["origin"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        a = _make_agent(tmp_path, role="assistant")
        msg = _msg("telegram", "777", "hello")
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)

        origin = captured["origin"]
        assert origin["message_type"] == MessageType.CHANNEL_IN.value
        assert origin["source"] == "telegram"
        assert origin["execution_role"] == "assistant"
        # No synthetic/button_answer markers were on msg.context — they
        # must not appear (not even as None) in the snapshot.
        assert "synthetic" not in origin
        assert "button_answer" not in origin

    async def test_voice_handoff_reservation_requires_trusted_commit(
        self, tmp_path,
    ):
        """Only a complete server-installed reservation crosses to tools."""
        import agent as agent_mod

        captured: dict[str, Any] = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["origin"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        class CompleteReservation:
            def reserve(self):
                return None

            def release(self):
                return None

            def commit(self, _job):
                return None

        a = _make_agent(tmp_path, role="concierge")
        complete = CompleteReservation()
        msg = _msg("voice", "scope-1", "hello")
        msg.context.update({
            "_voice_transport": "ws",
            "_voice_handoff_reservation": complete,
        })
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)
        assert captured["origin"]["_voice_handoff_reservation"] is complete

        incomplete = SimpleNamespace(reserve=lambda: None, release=lambda: None)
        msg.context["_voice_handoff_reservation"] = incomplete
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)
        assert "_voice_handoff_reservation" not in captured["origin"]

    async def test_origin_snapshot_copies_marker_keys_when_present(self, tmp_path):
        """synthetic/button_answer ride on msg.context (set by a LATER
        task's button-broker replay) and must be copied through verbatim."""
        import agent as agent_mod
        from bus import BusMessage, MessageType

        captured: dict[str, Any] = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["origin"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        a = _make_agent(tmp_path, role="assistant")
        msg = BusMessage(
            type=MessageType.CHANNEL_IN,
            source="telegram",
            target="assistant",
            content="yes",
            channel="telegram",
            context={
                "chat_id": "777", "synthetic": "button",
                "button_answer": "yes",
            },
        )
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)

        origin = captured["origin"]
        assert origin["synthetic"] == "button"
        assert origin["button_answer"] == "yes"

    async def test_origin_var_inherited_by_child_task(self, tmp_path):
        """A task spawned from inside receive_response must see the same
        origin value — this is the pattern delegate_to_agent relies on."""
        import asyncio as _asyncio
        import agent as agent_mod

        child_saw: dict[str, Any] = {}

        class SpawningClient(FakeClient):
            async def receive_response(self):
                async def _child():
                    child_saw["origin"] = agent_mod.origin_var.get(None)
                t = _asyncio.create_task(_child())
                await t
                async for m in super().receive_response():
                    yield m

        a = _make_agent(tmp_path, role="butler")
        msg = _msg("voice", "living-room", "lights on")
        with patch("sdk_client_pool._default_make_client", SpawningClient):
            await a._process(msg)

        assert child_saw["origin"] is not None
        assert child_saw["origin"]["channel"] == "voice"
        assert child_saw["origin"]["role"] == "butler"


# ---------------------------------------------------------------------------
# Scheduled-trigger silence (spec 2026-04-28 §3.2)
# ---------------------------------------------------------------------------


class _StubChannel:
    """Minimal channel double for handle_message dispatch tests.

    NOT a Channel subclass — Channel is ABC and instantiation would
    fail. The agent only does hasattr() checks plus direct attribute
    calls, so a duck-typed object suffices. ChannelManager.register
    keys on .name.
    """

    name = "telegram"
    default_agent = "assistant"

    def __init__(self) -> None:
        from unittest.mock import MagicMock, AsyncMock
        # create_on_token is a sync factory returning an async callback.
        self.create_on_token = MagicMock(return_value=AsyncMock())
        self.send = AsyncMock()
        self.send_response = AsyncMock()
        self.finalize_stream = AsyncMock()
        self.finalize_response_stream = AsyncMock()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _scheduled_msg(text: str = "tick") -> BusMessage:
    return BusMessage(
        type=MessageType.SCHEDULED,
        source="scheduler",
        target="assistant",
        content=text,
        channel="telegram",
        context={"chat_id": "interval-heartbeat"},
    )


def _request_msg(text: str = "hi") -> BusMessage:
    return BusMessage(
        type=MessageType.REQUEST,
        source="user",
        target="assistant",
        content=text,
        channel="telegram",
        context={"chat_id": "12345"},
    )


class TestScheduledSilence:
    async def test_scheduled_does_not_create_on_token(self, tmp_path):
        """Spec §3.2 B.1: SCHEDULED must NOT receive a streaming
        callback. The Telegram channel sends the first token as a new
        chat message; suppressing on_token is the load-bearing fix."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value=""),
        ):
            await agent.handle_message(_scheduled_msg())

        assert stub.create_on_token.call_count == 0

    async def test_request_still_streams(self, tmp_path):
        """Spec §3.2 B.1 non-goal: streaming for REQUEST is unchanged."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="pong"),
        ):
            await agent.handle_message(_request_msg())

        assert stub.create_on_token.call_count == 1

    async def test_event_wake_does_not_create_on_token(self, tmp_path):
        """#534 (Sol design r1): an event wake dispatches as CHANNEL_IN, so
        without this gate its narration STREAMS to the operator chat before
        the final-text sentinel suppression can run. Event-wake turns are
        buffered exactly like SCHEDULED ones; the `synthetic` marker is
        ingress-unforgeable (INV-EV-006)."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        msg = BusMessage(
            type=MessageType.CHANNEL_IN,
            source="telegram",
            target="assistant",
            content="wake",
            channel="telegram",
            context={"chat_id": "1", "synthetic": "event_wake"},
        )
        with patch.object(
            agent, "_process", AsyncMock(return_value="Event processed."),
        ):
            await agent.handle_message(msg)

        assert stub.create_on_token.call_count == 0

    async def test_silent_sentinel_suppresses_send(self, tmp_path):
        """Spec §3.2 B.2: text == '<silent/>' (after strip) must
        suppress channel.send / finalize_stream and produce no
        BusMessage downstream."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="<silent/>"),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None

    async def test_whitespace_suppresses_send(self, tmp_path):
        """Spec §3.2 B.2: empty / whitespace-only text suppresses
        delivery the same way as the explicit sentinel."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="   \n  "),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None

    async def test_sentinel_with_whitespace_suppresses_send(self, tmp_path):
        """A buffered model that emits the sentinel on its own line (or with
        leading/trailing whitespace) must still be suppressed — the gate
        strips surrounding whitespace around the sentinel."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="\n  <silent/>  \n"),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send.call_count == 0
        assert stub.send_response.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None

    async def test_repeated_sentinel_suppresses_send(self, tmp_path):
        """Output that is nothing but repeated sentinels + whitespace is
        silence, not a message."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="<silent/>\n<silent/>"),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send.call_count == 0
        assert stub.send_response.call_count == 0
        assert result is None

    async def test_recant_after_sentinel_is_still_sent(self, tmp_path):
        """G-3 recant contract preserved: a `<silent/>` followed by real prose
        is NOT swallowed — the residual message is delivered."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process",
            AsyncMock(return_value="<silent/> actually, meeting at 3pm."),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send_response.call_count == 1
        assert stub.send_response.call_args.args[0] == (
            "<silent/> actually, meeting at 3pm."
        )
        assert result is not None

    async def test_real_text_passes_through(self, tmp_path):
        """Spec §3.2 B.2: any non-sentinel, non-empty text reaches the
        channel and emits a RESPONSE BusMessage with the same body."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        text = "Meeting with Ana in 20 min."
        with patch.object(
            agent, "_process", AsyncMock(return_value=text),
        ):
            result = await agent.handle_message(_scheduled_msg())

        # No on_token (Fix B.1 disables streaming for SCHEDULED). A successful
        # scheduled turn (error_kind is None) routes through send_response
        # (v0.70.0 rich-text), NOT plain send/finalize_stream.
        assert stub.send_response.call_count == 1
        sent_text = stub.send_response.call_args.args[0]
        assert sent_text == text
        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is not None
        assert result.content == text

    async def test_silent_sentinel_suppresses_send_on_request_turn(
        self, tmp_path,
    ):
        """G-3 (v0.33.0, exploration2): on a USER-driven REQUEST turn,
        a bare `<silent/>` accumulated text must also suppress the send.

        Pre-fix the suppression was scoped to MessageType.SCHEDULED, so
        Ellen's outer turn after a configurator engagement (cid
        `dcc3c30b` 2026-05-01) leaked the literal sentinel into a user
        DM. Lifting the SCHEDULED-only gate generalizes the noop
        contract."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="<silent/>"),
        ):
            result = await agent.handle_message(_request_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        # M4 (v0.53.0): send suppression is still load-bearing, but a REQUEST
        # turn must ALWAYS return a RESPONSE (empty content) so bus.request()
        # callers (voice SSE/WS, /invoke) resolve instead of hanging 300s.
        assert result is not None
        assert result.type == MessageType.RESPONSE
        assert result.content == ""

    async def test_whitespace_suppresses_send_on_request_turn(self, tmp_path):
        """G-3 (v0.33.0): whitespace-only accumulated text on a
        USER-driven REQUEST turn must also suppress delivery, matching
        the SCHEDULED behavior."""
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="   \n  "),
        ):
            result = await agent.handle_message(_request_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        # M4 (v0.53.0): empty REQUEST turn still returns an empty RESPONSE.
        assert result is not None
        assert result.type == MessageType.RESPONSE
        assert result.content == ""

    async def test_empty_request_turn_resolves_pending_future(self, tmp_path):
        """M4/M6 (v0.53.0): a REQUEST turn whose output strips to <silent/>
        (or whose _process returns None — a tool-only turn with no TextBlocks)
        must resolve the caller's bus future PROMPTLY with an empty RESPONSE.

        Pre-fix, handle_message returned None and the bus never auto-responded
        to the REQUEST, so bus.request() blocked until its timeout (300s in
        prod on voice SSE/WS and /invoke). We use a short timeout and assert
        it resolves rather than raising TimeoutError.
        """
        from bus import BusMessage, MessageBus, MessageType

        for ret in ("<silent/>", None):
            agent = _make_agent(tmp_path, role="assistant")
            bus = MessageBus()
            bus.register("voice")
            bus.register("assistant", agent.handle_message)
            loop_task = asyncio.create_task(bus.run_agent_loop("assistant"))
            try:
                with patch.object(
                    agent, "_process", AsyncMock(return_value=ret),
                ):
                    req = BusMessage(
                        type=MessageType.REQUEST, source="voice",
                        target="assistant", content="turn off the lights",
                        channel="voice", context={"chat_id": "scope1"},
                    )
                    result = await bus.request(req, timeout=2)
                assert result.type == MessageType.RESPONSE
                assert result.content == ""
                assert result.reply_to == req.id
            finally:
                loop_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loop_task


# ---------------------------------------------------------------------------
# TestSaveBeforeOverwrite — spec §4.2 C1, Task 7
# ---------------------------------------------------------------------------


class TestSaveBeforeOverwrite:
    """_process schedules a background claim-free retain (via _spawn_cold_retain)
    when a cold prior session exists (spec §4.2 save-before-overwrite branch,
    tier model §2.4). The pure helper _resume_decision is already unit-tested in
    test_agent_save_path.py; this class covers the load-bearing call inside
    _process itself.

    NOTE (Task 8, tier model §2.4): the gap branch no longer awaits save_session
    synchronously. Doing so would race register() — save_session claims the registry
    entry via try_begin_save/finish_save, while register() overwrites the same
    channel_key. Instead _process captures the OLD sid before register() fires and
    hands it to _spawn_cold_retain, which runs retain_cold_session in the background
    (claim-free, registry-decoupled). This keeps per-item LLM classification off
    the turn's hot path. The original intent — "cold prior session IS persisted on
    the next turn" — is preserved; only the mechanism changed."""

    async def test_cold_path_schedules_background_retain_with_old_sid(
        self, tmp_path, monkeypatch,
    ):
        """When the registry holds a stale telegram entry (idle > 12 h),
        _resume_decision returns ("new", True) and _process must schedule a
        background retain via _spawn_cold_retain, passing the OLD sdk_session_id
        captured BEFORE register() overwrites the entry. save_session must NOT
        be called (it would race register() on the same channel_key)."""
        import agent as agent_mod
        from datetime import datetime, timedelta, timezone

        retain_calls: list[dict] = []

        async def _fake_retain_cold(old, *, directory, channel, semantic_memory,
                                    fence_generation=None):
            retain_calls.append({
                "sid": old.sdk_session_id,
                "role": old.agent,
                "directory": directory,
                "user_peer": old.user_provenance.user_peer if old.user_provenance else None,
                "channel": channel,
            })

        save_calls: list = []

        async def _fake_save(*a, **kw):
            save_calls.append(kw)

        monkeypatch.setattr(agent_mod, "retain_cold_session", _fake_retain_cold)
        monkeypatch.setattr(agent_mod, "save_session", _fake_save)
        FakeClient.reset()

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        # Inject a stale entry under the canonical resident identity (Task 9):
        # sdk_session_id present, last_active > 12 h ago → expired, retain_old.
        # Seeded via register() so it carries a real stored user_provenance.
        from personality_types import SpeakerProvenance
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=13)
        ).isoformat()
        cold_key = build_scoped_session_key("telegram", "assistant", "123")
        await reg.register(
            cold_key, resident_role_id("assistant"), "old-stale-sid",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("assistant"),
            user_provenance=SpeakerProvenance(speaker_kind="user", user_peer="nicola"),
        )
        reg._data[cold_key]["last_active"] = stale_ts

        agent = _make_agent_with_registry(reg, role="assistant")

        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_msg("telegram", "123", "hi"))

        # Drain any background tasks so retain_cold_session can complete.
        if agent._bg_tasks:
            await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        # save_session must NOT be called on the cold path (it races register()).
        assert save_calls == [], (
            f"save_session must NOT be called on the cold path (races register()); "
            f"got: {save_calls}"
        )
        # retain_cold_session must have been called with the OLD sid.
        assert len(retain_calls) == 1, (
            f"expected retain_cold_session called once on cold path; got {len(retain_calls)} calls"
        )
        call = retain_calls[0]
        assert call["sid"] == "old-stale-sid", call
        # Task 9/10: the snapshot carries the canonical role_id as ``agent``.
        assert call["role"] == "resident:assistant", call
        assert call["directory"].endswith("agent-home/assistant"), call
        assert call["user_peer"] == "nicola", call
        assert call["channel"] == "telegram", call

    async def test_save_session_not_called_on_resume_path(self, tmp_path, monkeypatch):
        """When the registry holds a fresh entry (idle < 12 h), _resume_decision
        returns ("resume", False) and _process must NOT call save_session."""
        import agent as agent_mod
        from datetime import datetime, timedelta, timezone

        save_calls: list[dict] = []

        async def _fake_save(channel_key, session_registry, semantic_memory,
                             *, directory, channel):
            save_calls.append({"channel_key": channel_key})

        monkeypatch.setattr(agent_mod, "save_session", _fake_save)
        FakeClient.reset()

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        # Fresh entry: last_active just 5 minutes ago → resume, no save.
        fresh_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        reg._data[build_scoped_session_key("telegram", "assistant", "456")] = {
            "agent": resident_role_id("assistant"),
            "sdk_session_id": "fresh-live-sid",
            "last_active": fresh_ts,
            "binding_digest": RESIDENT_DIGEST,
        }

        agent = _make_agent_with_registry(reg, role="assistant")

        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_msg("telegram", "456", "hi"))

        assert save_calls == [], (
            f"save_session must not be called on the resume path; got {save_calls}"
        )

    async def test_resumed_session_skips_overlay_and_recall(
        self, tmp_path, monkeypatch,
    ):
        """Spec §4.3: on a resumed session (is_fresh=False), _plan_load
        returns push_overlay=False, auto_recall=False. The overlay and
        per-turn recall are BOTH skipped — the overlay rides along on the
        resumed SDK thread; no auto-recall fires."""
        import agent as agent_mod
        from datetime import datetime, timedelta, timezone

        # Monkeypatch save_session so the save-before-overwrite path can't
        # accidentally fire and muddy the assertion.
        monkeypatch.setattr(agent_mod, "save_session", AsyncMock())
        FakeClient.reset()

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        # Seed a FRESH entry: sdk_session_id present, last_active just 5 min ago
        # → _resume_decision returns ("resume", False) → is_fresh=False.
        fresh_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        reg._data[build_scoped_session_key("telegram", "assistant", "789")] = {
            "agent": resident_role_id("assistant"),
            "sdk_session_id": "live-sid-resume",
            "last_active": fresh_ts,
            "binding_digest": RESIDENT_DIGEST,
        }

        fake = FakeSemanticMemory(overlay="should-not-appear", facts="should-not-appear")
        agent = _make_agent_with_registry(reg, role="assistant")
        # Replace the NoOp semantic memory with our tracking fake.
        agent._semantic_memory = fake

        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_msg("telegram", "789", "hi"))

        assert fake.profile_calls == [], (
            "profile() must not be called on a resumed session (overlay rides "
            f"along on the thread); got: {fake.profile_calls}"
        )
        assert fake.recall_calls == [], (
            "recall() must not be called on a resumed session (no auto-recall "
            f"on resume path); got: {fake.recall_calls}"
        )


# ---------------------------------------------------------------------------
# Structural contract: Agent.__init__ must not accept memory param
# ---------------------------------------------------------------------------


async def test_untrusted_webhook_trigger_gets_restricted_runtime(tmp_path):
    """Release A Layer 1: a webhook_trigger turn builds the locked-down runtime."""
    import json
    from agent import origin_var
    agent = _make_agent(tmp_path, role="assistant")
    token = origin_var.set({"_origin_route": "webhook_trigger"})
    try:
        options = await agent._build_options(
            channel="webhook", channel_key="webhook-assistant-uuid",
            is_fresh=True, resume_sid=None, user_text="voicemail from a caller",
        )
    finally:
        origin_var.reset(token)
    assert options.tools == []
    assert options.plugins == []
    assert options.setting_sources == []
    assert json.loads(options.settings) == {"disableAllHooks": True}
    assert set(options.allowed_tools) == {
        "mcp__casa-framework__recall_memory",
        "mcp__casa-framework__send_message",
    }


async def test_webhook_missing_route_is_restricted_fail_closed(tmp_path):
    """Fail-closed: a webhook turn with NO origin route (marker regression) is
    still restricted — never the full runtime."""
    from agent import origin_var
    agent = _make_agent(tmp_path, role="assistant")
    token = origin_var.set({})  # no _origin_route
    try:
        options = await agent._build_options(
            channel="webhook", channel_key="webhook-assistant-x",
            is_fresh=True, resume_sid=None, user_text="x",
        )
    finally:
        origin_var.reset(token)
    assert options.tools == []
    assert options.setting_sources == []


# ---------------------------------------------------------------------------
# #521 — setup-episode execution-outcome correlation
# ---------------------------------------------------------------------------

_SETUP_NS = "mcp__plugin_gm_gm__setup_gm"


def _mk_init(sid: str, tools: list | None):
    from claude_agent_sdk import SystemMessage as _SDKSystemMessage
    data = {"session_id": sid}
    if tools is not None:
        data["tools"] = tools
    try:
        return _SDKSystemMessage(subtype="init", data=data)
    except TypeError:
        m = _SDKSystemMessage.__new__(_SDKSystemMessage)
        m.subtype = "init"          # type: ignore[attr-defined]
        m.data = data               # type: ignore[attr-defined]
        return m


def _setup_msg(chat_id: str, episode: str = "ep-521") -> BusMessage:
    m = _msg("telegram", chat_id, text="[casa plugin setup] run it")
    m.context.update({"synthetic": "plugin_setup", "setup_episode": episode})
    return m


@contextmanager
def _capture_reports(monkeypatch=None):
    """Patch plugin_setup_episodes.report_dispatch_outcome, capturing calls."""
    import plugin_setup_episodes as pse
    calls: list[tuple[str, dict]] = []

    def _report(episode_id, **kw):
        calls.append((episode_id, kw))

    with patch.object(pse, "report_dispatch_outcome", _report):
        yield calls


class TestSetupOutcomeReport:
    async def test_tool_ran_reports_used_ok(self, tmp_path):
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_init("sid-a", ["Read", _SETUP_NS]),
            _mk_tool_use("t1", _SETUP_NS),
            _mk_tool_result("t1", is_error=False, text="wired"),
            _mk_assistant("Setup done."),
            _mk_result("sid-a"),
        ])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            await agent._process(_setup_msg("op-1"))
        assert calls == [("ep-521", {
            "tools_used_ok": {_SETUP_NS},
            "tools_attempted": {_SETUP_NS},
            "available_tools": {"Read", _SETUP_NS},
        })]

    async def test_tool_absent_reports_availability_without_it(self, tmp_path):
        # The observed #521 turn: zero tool uses, init lacking the tool.
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_init("sid-b", ["Read"]),
            _mk_assistant("I do not seem to have that tool."),
            _mk_result("sid-b"),
        ])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            await agent._process(_setup_msg("op-2"))
        assert calls == [("ep-521", {
            "tools_used_ok": set(),
            "tools_attempted": set(),
            "available_tools": {"Read"},
        })]

    async def test_no_init_reports_unknown_availability(self, tmp_path):
        # Warm-reuse shape: no init replay for the turn.
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_assistant("done?"),
            _mk_result("sid-c"),
        ])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            await agent._process(_setup_msg("op-3"))
        assert calls[0][1]["available_tools"] is None

    async def test_errored_call_is_attempted_not_used_ok(self, tmp_path):
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_init("sid-d", [_SETUP_NS]),
            _mk_tool_use("t1", _SETUP_NS),
            _mk_tool_result("t1", is_error=True, text="denied"),
            _mk_assistant("It was denied."),
            _mk_result("sid-d"),
        ])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            await agent._process(_setup_msg("op-4"))
        assert calls[0][1]["tools_used_ok"] == set()
        assert calls[0][1]["tools_attempted"] == {_SETUP_NS}

    async def test_normal_turn_never_reports(self, tmp_path):
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_assistant("pong"),
            _mk_result("sid-e"),
        ])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            await agent._process(_msg("telegram", "op-5"))
        assert calls == []

    async def test_raising_turn_still_reports(self, tmp_path):
        # Terra design r1 S1: a turn whose every SDK attempt raises skips
        # the tail of _process — the report must still run (finally).
        agent = _make_agent(tmp_path)
        boom = type("CLIConnectionError", (RuntimeError,), {})
        ScriptedToolClient.reset(
            *[[boom("attempt dead")] for _ in range(6)])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), patch_retry_sleep(), \
                _capture_reports() as calls:
            with pytest.raises(Exception):
                await agent._process(_setup_msg("op-6"))
        assert len(calls) == 1
        assert calls[0][1]["tools_used_ok"] == set()

    async def test_cancelled_turn_still_reports(self, tmp_path):
        # Sol design r1 S1: role teardown cancels in-flight dispatches;
        # the collected evidence must be reported before the cancel
        # propagates, or the row rests dispatched with no re-arm path.
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_init("sid-f", [_SETUP_NS]),
            _mk_tool_use("t1", _SETUP_NS),
            _mk_tool_result("t1", is_error=False, text="wired"),
            asyncio.CancelledError(),
        ])
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            with pytest.raises(asyncio.CancelledError):
                await agent._process(_setup_msg("op-7"))
        assert len(calls) == 1
        # Evidence collected before the cancel still counts (tool DID run).
        assert calls[0][1]["tools_used_ok"] == {_SETUP_NS}

    async def test_bypass_path_turn_reports_evidence(self, tmp_path):
        # Sol diff r1 hardening: the per-turn BYPASS path (SCHEDULED turns
        # never pool) must feed the same evidence accumulator — pins the
        # bypass closure's turn_state["states"] append.
        agent = _make_agent(tmp_path)
        ScriptedToolClient.reset([
            _mk_init("sid-h", [_SETUP_NS]),
            _mk_tool_use("t1", _SETUP_NS),
            _mk_tool_result("t1", is_error=False, text="wired"),
            _mk_assistant("done"),
            _mk_result("sid-h"),
        ])
        msg = _setup_msg("op-9")
        msg.type = MessageType.SCHEDULED
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), _capture_reports() as calls:
            await agent._process(msg)
        assert calls[0][1]["tools_used_ok"] == {_SETUP_NS}

    async def test_evidence_aggregates_across_sdk_attempts(self, tmp_path):
        # Terra design r1 S2: the tool can run in an attempt that then
        # fails retryably; the successful later attempt has no evidence.
        # Aggregation across attempts must keep the used_ok fact.
        agent = _make_agent(tmp_path)
        boom = type("CLIConnectionError", (RuntimeError,), {})
        ScriptedToolClient.reset(
            [
                _mk_init("sid-g", [_SETUP_NS]),
                _mk_tool_use("t1", _SETUP_NS),
                _mk_tool_result("t1", is_error=False, text="wired"),
                boom("stream died"),
            ],
            [
                _mk_assistant("(retried) done"),
                _mk_result("sid-g2"),
            ],
        )
        with patch("sdk_client_pool._default_make_client",
                   ScriptedToolClient), patch_retry_sleep(), \
                _capture_reports() as calls:
            await agent._process(_setup_msg("op-8"))
        assert calls[0][1]["tools_used_ok"] == {_SETUP_NS}
        assert calls[0][1]["available_tools"] == {_SETUP_NS}


# ---------------------------------------------------------------------------
# Red cases — #650 (specified by reviewer, accepted before any production
# change; see drive run 2026-08-18-b, cluster 650). Once accepted these are
# FROZEN — re-specify + re-accept rather than edit.
# ---------------------------------------------------------------------------


class _RecordingFinalDeliveryChannel:
    """Channel double recording FINAL deliveries only.

    Deliberately no ``create_on_token``: the assertion targets the final
    channel delivery, not the token stream. Both ``send_response`` (the
    error_kind-None branch) and ``send`` (the classified-error branch)
    record into the same list so the count is branch-agnostic.
    """

    name = "telegram"

    def __init__(self) -> None:
        self.deliveries: list[str] = []

    async def send_response(self, text: str, context: dict) -> None:
        self.deliveries.append(text)

    async def send(self, text: str, context: dict) -> None:
        self.deliveries.append(text)


class TestIssue650RedCases:
    """#650: a trusted ask that consumes retries must not end invisibly, and a
    session whose resumed trusted asks repeatedly exhaust SDK retries must not
    be resumed indefinitely.

    Provenance predating this run: issue #650 (defects (a)/(b), filed against
    v0.220.0; the six primary files are byte-identical at 8f8f593e), and the
    re-survey dossier facts anchored at 8f8f593e — the sentinel gate
    (agent.py:845-854) suppresses a retry-tainted final silence to zero
    deliveries, and only REFUSAL clears a resumed session (agent.py:1386).
    """

    @pytest.mark.parametrize("silent_final", ["", "<silent/>"])
    @pytest.mark.parametrize("trusted", [True, False])
    async def test_trusted_retry_then_silent_final_emits_exactly_one_delivery(
        self, tmp_path, silent_final, trusted,
    ):
        """One retryable fault, then a silent final attempt: a trusted ask
        gets exactly one non-empty delivery; an author-less turn stays
        doctrinally silent (zero deliveries)."""
        from ingress_identity import ingress_identity

        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.reset()
        FakeClient.failure_schedule = [
            CLIConnectionError("upstream reset"), None,
        ]
        FakeClient.response_text = silent_final

        agent = _make_agent(tmp_path, role="assistant")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        msg = _msg("telegram", "retry-silent", "status?")
        if trusted:
            msg.trusted_user_origin = ingress_identity(
                "telegram", sender_id="9001", sender_is_operator=True,
            )

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep() as sleep:
            await agent.handle_message(msg)

        assert FakeClient.attempts == 2
        assert sleep.await_count == 1
        if trusted:
            assert len(channel.deliveries) == 1
            assert channel.deliveries[0].strip()
        else:
            assert len(channel.deliveries) == 0

    async def test_two_trusted_sdk_retry_exhaustions_start_fresh_and_retain_old_snapshot(
        self, tmp_path,
    ):
        """Two consecutive trusted asks that exhaust SDK retries against the
        same resumed sid: the third ask must NOT resume it — fresh decision,
        old snapshot retained."""
        from error_kinds import ErrorKind, _USER_MESSAGES
        from ingress_identity import ingress_identity

        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        resumes: list[str | None] = []

        class _ResumeRecordingClient(FakeClient):
            def __init__(self, options):
                resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            CLIConnectionError(f"upstream reset {i}") for i in range(6)
        ] + [None]
        FakeClient.response_text = "recovered"

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register(
            build_scoped_session_key("telegram", "butler", "fault-scope"),
            resident_role_id("butler"), "old-sid",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        agent = _make_agent_with_registry(reg, role="butler")

        retain_calls: list[Any] = []
        agent._spawn_cold_retain = (  # type: ignore[method-assign]
            lambda old, **kw: retain_calls.append(old)
        )

        def _trusted_msg() -> BusMessage:
            m = _msg("telegram", "fault-scope", "status?")
            m.trusted_user_origin = ingress_identity(
                "telegram", sender_id="9001", sender_is_operator=True,
            )
            return m

        with patch(
            "sdk_client_pool._default_make_client", _ResumeRecordingClient,
        ), patch_retry_sleep() as sleep:
            first = await agent.handle_message(_trusted_msg())
            second = await agent.handle_message(_trusted_msg())

            assert first is not None
            assert second is not None
            assert first.content == _USER_MESSAGES[ErrorKind.SDK_ERROR]
            assert second.content == _USER_MESSAGES[ErrorKind.SDK_ERROR]
            assert FakeClient.attempts == 6
            assert sleep.await_count == 4
            assert resumes == ["old-sid"] * 6
            assert retain_calls == []

            third = await agent.handle_message(_trusted_msg())

        assert third is not None
        assert third.content == "recovered"
        assert FakeClient.attempts == 7
        assert resumes == ["old-sid"] * 6 + [None]
        assert len(retain_calls) == 1
        assert retain_calls[0].sdk_session_id == "old-sid"

    async def test_untrusted_sdk_retry_exhaustions_keep_resuming(
        self, tmp_path,
    ):
        """Scoping pin: the identical six-failure/third-success sequence
        WITHOUT trusted ingress keeps resuming and retains nothing — an
        author-less turn must not burn a conversation's resume streak."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        resumes: list[str | None] = []

        class _ResumeRecordingClient(FakeClient):
            def __init__(self, options):
                resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            CLIConnectionError(f"upstream reset {i}") for i in range(6)
        ] + [None]
        FakeClient.response_text = "recovered"

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register(
            build_scoped_session_key("telegram", "butler", "fault-scope"),
            resident_role_id("butler"), "old-sid",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        agent = _make_agent_with_registry(reg, role="butler")

        retain_calls: list[Any] = []
        agent._spawn_cold_retain = (  # type: ignore[method-assign]
            lambda old, **kw: retain_calls.append(old)
        )

        with patch(
            "sdk_client_pool._default_make_client", _ResumeRecordingClient,
        ), patch_retry_sleep():
            for _ in range(3):
                await agent.handle_message(_msg("telegram", "fault-scope", "status?"))

        assert FakeClient.attempts == 7
        assert resumes == ["old-sid"] * 7
        assert retain_calls == []

    @pytest.mark.parametrize("silent_final", ["", "<silent/>"])
    async def test_trusted_silence_without_retries_stays_silent(
        self, tmp_path, silent_final,
    ):
        """Acceptance control (Sol, redcase round 2): a trusted ask whose turn
        consumed NO retries and ended silent is doctrinal silence — zero
        deliveries. Kills the mutant that notices every trusted silent turn
        without checking retry consumption."""
        from ingress_identity import ingress_identity

        FakeClient.reset()
        FakeClient.failure_schedule = [None]
        FakeClient.response_text = silent_final

        agent = _make_agent(tmp_path, role="assistant")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        msg = _msg("telegram", "retry-silent", "status?")
        msg.trusted_user_origin = ingress_identity(
            "telegram", sender_id="9001", sender_is_operator=True,
        )

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep() as sleep:
            await agent.handle_message(msg)

        assert FakeClient.attempts == 1
        assert sleep.await_count == 0
        assert len(channel.deliveries) == 0


class TestDeliveryAcknowledgedAnnouncements:
    """#701: a durable announcement is discharged by DELIVERY, not by a turn.

    The seam is the one #556 already established — a normal return from a
    delivery method proves nothing, because every one of them returns normally
    when the PTB app is absent, having made zero Bot API calls. So the channel's
    own outcome decides, and every ambiguous answer keeps the obligation.
    """

    def _agent(self, tmp_path):
        agent = _make_agent(tmp_path, role="assistant")
        stub = _StubTelegramChannel()
        agent._channel_manager.register(stub)
        return agent, stub

    def _notice(self, acks, text="narrated"):
        async def _ack() -> None:
            acks.append(text)
        return _ack

    @pytest.mark.parametrize(
        "outcome,acknowledged",
        [
            (DeliveryOutcome.DELIVERED, True),
            (DeliveryOutcome.NOT_DELIVERED, False),
            (DeliveryOutcome.UNKNOWN, False),
            (None, False),
        ],
        ids=["delivered", "not-delivered", "unknown", "off-contract"],
    )
    async def test_only_a_transport_confirmed_delivery_acknowledges(
        self, tmp_path, outcome, acknowledged,
    ):
        agent, stub = self._agent(tmp_path)
        stub.finalize_response_stream.return_value = outcome
        acks: list = []
        msg = _msg("telegram", "123", "hi")
        msg.on_delivery = self._notice(acks)

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(msg)

        assert stub.finalize_response_stream.call_count == 1
        assert acks == (["narrated"] if acknowledged else [])

    async def test_a_head_that_landed_acknowledges_before_the_tail_raises(
        self, tmp_path,
    ):
        """A reply whose first page reached Telegram HAS announced the
        delegation, whatever happens to page three."""
        agent, stub = self._agent(tmp_path)
        acks: list = []
        msg = _msg("telegram", "123", "hi")
        msg.on_delivery = self._notice(acks)

        async def _head_then_raise(text, context, on_token):
            context["_delivery_head_sent"] = True
            raise RuntimeError("page three failed")

        stub.finalize_response_stream.side_effect = _head_then_raise

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            with pytest.raises(RuntimeError):
                await agent.handle_message(msg)

        assert acks == ["narrated"]

    async def test_a_raise_before_the_head_acknowledges_nothing(self, tmp_path):
        agent, stub = self._agent(tmp_path)
        acks: list = []
        msg = _msg("telegram", "123", "hi")
        msg.on_delivery = self._notice(acks)
        stub.finalize_response_stream.side_effect = RuntimeError("channel down")

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            with pytest.raises(RuntimeError):
                await agent.handle_message(msg)

        assert acks == []

    async def test_a_silent_turn_acknowledges_nothing(self, tmp_path):
        agent, stub = self._agent(tmp_path)
        acks: list = []
        msg = _msg("telegram", "123", "hi")
        msg.on_delivery = self._notice(acks)

        with patch.object(agent, "_process",
                          AsyncMock(return_value="<silent/>")):
            await agent.handle_message(msg)

        assert stub.finalize_response_stream.call_count == 0
        assert acks == []

    async def test_a_generic_error_reply_acknowledges_nothing(self, tmp_path):
        """The text that reached the operator is the turn-failure notice, not
        the delegation's narration. They were told something broke; they were
        not told what their delegation did."""
        agent, stub = self._agent(tmp_path)
        stub.finalize_stream.return_value = DeliveryOutcome.DELIVERED
        stub.send.return_value = DeliveryOutcome.DELIVERED
        acks: list = []
        msg = _msg("telegram", "123", "hi")
        msg.on_delivery = self._notice(acks)

        with patch.object(agent, "_process",
                          AsyncMock(side_effect=RuntimeError("turn exploded"))):
            await agent.handle_message(msg)

        # Something WAS delivered — just not the announcement.
        assert (stub.finalize_stream.call_count + stub.send.call_count) == 1
        assert acks == []

    async def test_a_failing_acknowledgement_leaves_the_obligation_owed(
        self, tmp_path, caplog,
    ):
        agent, stub = self._agent(tmp_path)
        stub.finalize_response_stream.return_value = DeliveryOutcome.DELIVERED
        msg = _msg("telegram", "123", "hi")
        calls: list = []

        async def _ack() -> None:
            calls.append(1)
            raise OSError("snapshot write failed")

        msg.on_delivery = _ack
        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(msg)

        # The turn is not broken by it, and the marker was never cleared.
        assert calls == [1]

    async def test_the_acknowledgement_fires_at_most_once(self, tmp_path):
        agent, stub = self._agent(tmp_path)
        stub.finalize_response_stream.return_value = DeliveryOutcome.DELIVERED
        acks: list = []
        msg = _msg("telegram", "123", "hi")
        msg.on_delivery = self._notice(acks)

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(msg)
            await agent.handle_message(msg)

        assert acks == ["narrated"]

    async def test_an_ordinary_turn_carries_no_obligation(self, tmp_path):
        agent, stub = self._agent(tmp_path)
        stub.finalize_response_stream.return_value = DeliveryOutcome.DELIVERED
        msg = _msg("telegram", "123", "hi")
        assert msg.on_delivery is None

        with patch.object(agent, "_process", AsyncMock(return_value="hello")):
            await agent.handle_message(msg)
        assert msg.on_delivery is None
