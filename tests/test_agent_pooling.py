"""Agent ↔ SdkClientPool integration (spec §4/§5, AR-1..AR-10).

Drives ``Agent.handle_message`` end-to-end with a scripted client factory
patched at ``sdk_client_pool._default_make_client`` (the seam BOTH the pooled
path and the per-turn bypass path construct their client through). Covers the
eligibility gate, warm reuse, the SCHEDULED / webhook-uuid / kill-switch
bypasses, the reset-listener flush, options fresh-vs-resumed, and ``aclose``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ClaudeAgentOptions,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from semantic_memory import SemanticMemory
from session_registry import SessionRegistry, build_scoped_session_key
from session_reg_helpers import RESIDENT_DIGEST, resident_prov, resident_role_id

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


# --------------------------------------------------------------------------
# SDK-message helpers (SDK-shape-tolerant, mirror test_sdk_client_pool_*)
# --------------------------------------------------------------------------


def _mk_text_block(text: str) -> _SDKTextBlock:
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


def _mk_result(sid, usage=None, *, is_error=False, result=""):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = is_error
    m.result = result
    if usage is not None:
        m.usage = usage
    return m


class ScriptedClient:
    """Warm-transport double: a stable session_id so a resumed turn reuses it.

    Auto-responds to every ``query`` with one AssistantMessage + a
    ResultMessage carrying the stable sid, so a turn always succeeds.
    """

    def __init__(self, options, sid: str = "sid-scripted") -> None:
        self.options = options
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []
        self._sid = sid

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        text = self.queries[-1] if self.queries else ""
        yield _mk_assistant("ok: " + text)
        yield _mk_result(self._sid)


class ScriptedFactory:
    """``make_client(options)`` factory counting constructions."""

    def __init__(self) -> None:
        self.constructed = 0
        self.clients: list[ScriptedClient] = []

    def __call__(self, options) -> ScriptedClient:
        self.constructed += 1
        c = ScriptedClient(options)
        self.clients.append(c)
        return c


class FakeSemanticMemory(SemanticMemory):
    """Overlay/recall double — overlay rendered as <peer_overlay>."""

    def __init__(self, overlay: str = "OV", facts: str = "") -> None:
        self._overlay = overlay
        self._facts = facts

    async def retain(self, bank, items, *, async_=True):
        return None

    async def recall(self, bank, query, *, tags, max_tokens,
                     types=("world", "experience", "observation"),
                     tags_match="any", budget="mid"):
        return self._facts

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        if not self._facts:
            return ()
        from personality_types import RecallHit
        return (RecallHit(
            text=self._facts, memory_type="world", sensitivity="friends",
            application_tags=(), provenance=None, backend_id="b1", document_id=None,
            chunk_id=None, source_fact_ids=None, metadata=None, context=None,
            score=None,
        ),)

    async def profile(self, bank):
        return self._overlay


@pytest.fixture
def scripted_factory(monkeypatch):
    factory = ScriptedFactory()
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    return factory


@pytest.fixture
async def agent_fixture(tmp_path, scripted_factory):
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        role_id=resident_role_id("assistant"),
        kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("assistant"),
    )
    agent = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=FakeSemanticMemory(overlay="OV"),
    )

    async def send_turn(text, *, msg_type=MessageType.REQUEST,
                        channel="telegram", chat_id="42"):
        msg = BusMessage(
            type=msg_type, source="user", target="assistant",
            content=text, channel=channel, context={"chat_id": chat_id},
        )
        return await agent.handle_message(msg)

    yield agent, send_turn
    # Teardown: close the pool so the per-test sdk-pool-sweeper task and any
    # warm ScriptedClients die with the test (isolation — no sweeper leaks
    # into a later test). aclose is idempotent, so tests that already call it
    # (e.g. test_aclose_closes_pool_and_unsubscribes) are unaffected.
    await agent.aclose()


async def _exercise_blocked_session_publish(
    agent, send_turn, monkeypatch, *, invalidate: bool,
):
    """Hold the real SessionRegistry lock after the SDK result is complete."""
    sdk_turn_finished = asyncio.Event()

    class PublishAwareClient(ScriptedClient):
        def __init__(self, options, *, sid, signal_finish):
            super().__init__(options, sid=sid)
            self._signal_finish = signal_finish

        async def receive_response(self):
            async for message in super().receive_response():
                yield message
            if self._signal_finish:
                sdk_turn_finished.set()

    class PublishAwareFactory:
        def __init__(self):
            self.clients = []

        @property
        def constructed(self):
            return len(self.clients)

        def __call__(self, options):
            generation = len(self.clients) + 1
            client = PublishAwareClient(
                options,
                sid=f"sid-{generation}",
                signal_finish=generation == 1,
            )
            self.clients.append(client)
            return client

    factory = PublishAwareFactory()
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    registry = agent._session_registry
    key = build_scoped_session_key("telegram", "assistant", "42")
    await registry._lock.acquire()
    gate_held = True
    first = asyncio.create_task(send_turn("first"))
    invalidation = None
    replacement = None
    try:
        await asyncio.wait_for(sdk_turn_finished.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not first.done()

        if invalidate:
            invalidation = asyncio.create_task(agent._pool.invalidate_all())
        replacement = asyncio.create_task(send_turn("second"))
        for _ in range(10):
            await asyncio.sleep(0)
            if factory.constructed > 1:
                break

        # The first SDK turn has a sid, but its durable registry publication
        # is still blocked. Neither ordinary same-key serialization nor an
        # invalidation handoff may let a second generation decide/build yet.
        assert factory.constructed == 1
        assert not replacement.done()
        if invalidation is not None:
            assert not invalidation.done()

        registry._lock.release()
        gate_held = False
        await asyncio.wait_for(first, timeout=1)
        pending = [replacement]
        if invalidation is not None:
            pending.append(invalidation)
        await asyncio.wait_for(asyncio.gather(*pending), timeout=1)

        if invalidate:
            assert factory.constructed == 2
            assert factory.clients[1].options.resume == "sid-1"
            assert registry.get(key)["sdk_session_id"] == "sid-2"
        else:
            assert factory.constructed == 1
            assert factory.clients[0].queries[-1].endswith("second")
            assert registry.get(key)["sdk_session_id"] == "sid-1"
    finally:
        if gate_held:
            registry._lock.release()
        tasks = [
            task for task in (first, invalidation, replacement)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_second_turn_reuses_warm_client(agent_fixture, scripted_factory):
    """Two REQUEST turns on one telegram chat → exactly ONE construction."""
    agent, send_turn = agent_fixture
    await send_turn("hello")
    await send_turn("again")
    assert scripted_factory.constructed == 1
    assert scripted_factory.clients[0].queries[-1].endswith("again")


async def test_invalidation_waits_for_pooled_session_publication(
    agent_fixture, monkeypatch,
):
    agent, send_turn = agent_fixture
    await _exercise_blocked_session_publish(
        agent, send_turn, monkeypatch, invalidate=True,
    )


async def test_concurrent_turn_waits_for_pooled_session_publication(
    agent_fixture, monkeypatch,
):
    agent, send_turn = agent_fixture
    await _exercise_blocked_session_publish(
        agent, send_turn, monkeypatch, invalidate=False,
    )


async def test_pooled_session_publish_failure_drops_unpublished_generation(
    agent_fixture, scripted_factory, monkeypatch,
):
    agent, send_turn = agent_fixture

    class PublishFailure(RuntimeError):
        pass

    async def fail_register(*args, **kwargs):
        raise PublishFailure("disk unavailable")

    monkeypatch.setattr(agent._session_registry, "register", fail_register)

    # handle_message converts processing failures into an error response; the
    # pool invariant is that the unpublished generation is no longer warm.
    await send_turn("hello")

    assert agent._pool.stats()["entries"] == 0
    assert scripted_factory.clients[0].disconnected


async def test_cancelled_pooled_session_publish_drops_unpublished_generation(
    agent_fixture, scripted_factory, monkeypatch,
):
    """#418: the old precondition polled ``state == "warm"`` on a 5s
    wall-clock budget and FELL THROUGH silently when a loaded xdist worker
    starved the loop — the test then failed downstream on asserts it never
    earned (``clients[0]`` on an empty list). Replace the poll with a
    deterministic barrier: ``register()`` signals on ENTRY, then blocks on
    the registry lock the test holds — cancellation is pinned INSIDE
    publication by construction, and a missed precondition fails loudly."""
    agent, send_turn = agent_fixture
    registry = agent._session_registry
    entered_publish = asyncio.Event()
    real_register = registry.register

    async def signaled_register(*args, **kwargs):
        entered_publish.set()
        return await real_register(*args, **kwargs)  # blocks on the held lock

    monkeypatch.setattr(registry, "register", signaled_register)
    await registry._lock.acquire()
    task = asyncio.create_task(send_turn("hello"))
    try:
        try:
            await asyncio.wait_for(entered_publish.wait(), timeout=30)
        except asyncio.TimeoutError:
            pytest.fail("turn never reached session publication within 30s")
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert agent._pool.stats()["entries"] == 0
        assert scripted_factory.clients[0].disconnected
    finally:
        registry._lock.release()
        await asyncio.gather(task, return_exceptions=True)


async def test_successful_pooled_turn_registers_once_per_turn(
    agent_fixture, monkeypatch,
):
    agent, send_turn = agent_fixture
    real_register = agent._session_registry.register
    registrations = []

    async def counted_register(*args, **kwargs):
        registrations.append((args, kwargs))
        await real_register(*args, **kwargs)

    monkeypatch.setattr(
        agent._session_registry, "register", counted_register,
    )

    await send_turn("hello")
    await send_turn("again")

    assert len(registrations) == 2


async def test_scheduled_turn_bypasses_pool(agent_fixture, scripted_factory):
    agent, send_turn = agent_fixture
    await send_turn("cron tick", msg_type=MessageType.SCHEDULED)
    await send_turn("cron tick", msg_type=MessageType.SCHEDULED)
    assert scripted_factory.constructed == 2          # per-turn clients
    assert agent._pool.stats()["entries"] == 0


async def test_webhook_uuid_turn_bypasses_pool(agent_fixture, scripted_factory):
    agent, send_turn = agent_fixture
    await send_turn("one-shot", channel="webhook",
                    chat_id="0b6f5df2-30cc-4f7e-9b4e-1d1c8f0a1b2c")
    assert agent._pool.stats()["entries"] == 0


async def test_new_reset_listener_closes_warm_entry(agent_fixture,
                                                    scripted_factory):
    agent, send_turn = agent_fixture
    await send_turn("hello")
    assert agent._pool.stats()["entries"] == 1
    await agent._session_registry.notify_reset(
        build_scoped_session_key("telegram", "assistant", "42"),
    )
    assert agent._pool.stats()["entries"] == 0
    assert scripted_factory.clients[0].disconnected


async def test_invalidate_all_closes_entries_but_pool_remains_usable(
    agent_fixture, scripted_factory,
):
    agent, send_turn = agent_fixture
    await send_turn("hello")
    key = build_scoped_session_key("telegram", "assistant", "42")
    old = agent._pool._entries[key]
    sweeper = agent._pool._sweeper

    await agent._pool.invalidate_all()

    assert old.state == "closed"
    assert scripted_factory.clients[0].disconnected
    assert agent._pool.stats() == {"entries": 0, "closing": False}
    assert agent._pool._sweeper is sweeper
    assert sweeper is not None and not sweeper.done()

    await send_turn("again")
    assert agent._pool.stats() == {"entries": 1, "closing": False}
    assert agent._pool._entries[key] is not old
    assert scripted_factory.constructed == 2


async def test_invalidate_all_does_not_close_a_concurrent_new_generation(
    agent_fixture, scripted_factory,
):
    agent, send_turn = agent_fixture
    await send_turn("hello")
    key = build_scoped_session_key("telegram", "assistant", "42")
    old = agent._pool._entries[key]
    old_client = scripted_factory.clients[0]
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def gated_disconnect():
        old_client.disconnected = True
        close_started.set()
        await release_close.wait()

    old_client.disconnect = gated_disconnect
    invalidation = asyncio.create_task(agent._pool.invalidate_all())
    await asyncio.wait_for(close_started.wait(), timeout=1)

    await send_turn("again")
    current = agent._pool._entries[key]
    assert current is not old
    assert current.state == "warm"
    assert not invalidation.done()

    release_close.set()
    await invalidation
    assert agent._pool._entries[key] is current
    assert current.state == "warm"


async def test_invalidate_all_waits_for_an_in_flight_entry_lock(
    agent_fixture, scripted_factory,
):
    agent, send_turn = agent_fixture
    await send_turn("hello")
    key = build_scoped_session_key("telegram", "assistant", "42")
    old = agent._pool._entries[key]
    await old.lock.acquire()

    invalidation = asyncio.create_task(agent._pool.invalidate_all())
    await asyncio.sleep(0)

    assert key not in agent._pool._entries
    assert old.state == "warm"
    assert not scripted_factory.clients[0].disconnected

    old.lock.release()
    await invalidation
    assert old.state == "closed"
    assert scripted_factory.clients[0].disconnected


async def test_agent_invalidate_tool_surface_closes_current_pool_generation(
    agent_fixture, scripted_factory,
):
    agent, send_turn = agent_fixture
    await send_turn("hello")
    old = next(iter(agent._pool._entries.values()))

    await agent.invalidate_tool_surface()

    assert old.state == "closed"
    assert scripted_factory.clients[0].disconnected
    assert agent._pool.stats() == {"entries": 0, "closing": False}


async def test_facade_schema_refresh_reconnects_only_butler_with_new_config(
    tmp_path, scripted_factory,
):
    from casa_core import wire_tina_ha_facade

    registry = McpServerRegistry()
    raw_config = {
        "type": "http",
        "url": "http://raw",
        "headers": None,
    }
    old_facade_config = {"type": "sdk", "instance": object()}
    new_facade_config = {"type": "sdk", "instance": object()}
    registry.register_http("homeassistant", "http://raw")
    registry.register_role_sdk(
        "homeassistant", "butler", old_facade_config,
    )
    sessions = SessionRegistry(str(tmp_path / "sessions.json"))

    def make_agent(role):
        return Agent(
            config=AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
                role=role,
                model="claude-sonnet-4-6",
                system_prompt="You are helpful.",
                character=CharacterConfig(name=role.title()),
                tools=ToolsConfig(
                    allowed=["Read", "mcp__homeassistant"],
                    permission_mode="acceptEdits",
                ),
                mcp_server_names=["homeassistant"],
                memory=MemoryConfig(
                    token_budget=1000, read_strategy="per_turn",
                ),
                role_id=resident_role_id(role),
                kind="resident",
                binding_digest=RESIDENT_DIGEST,
                speaker_provenance=resident_prov(role),
            ),
            session_registry=sessions,
            mcp_registry=registry,
            channel_manager=ChannelManager(),
            semantic_memory=FakeSemanticMemory(),
        )

    butler = make_agent("butler")
    assistant = make_agent("assistant")

    async def send(agent, role, text):
        return await agent.handle_message(BusMessage(
            type=MessageType.REQUEST,
            source="user",
            target=role,
            content=text,
            channel="telegram",
            context={"chat_id": "42"},
        ))

    try:
        await send(butler, "butler", "before")
        await send(assistant, "assistant", "before")
        old_butler_client, assistant_client = scripted_factory.clients
        assert (
            old_butler_client.options.mcp_servers["homeassistant"]
            is old_facade_config
        )
        assert assistant_client.options.mcp_servers["homeassistant"] == raw_config

        facade = type(
            "Facade", (), {"server_config": new_facade_config},
        )()
        await wire_tina_ha_facade(
            registry,
            facade,
            {"butler": butler, "assistant": assistant},
        )

        assert old_butler_client.disconnected
        assert butler._pool.stats()["entries"] == 0
        assert not assistant_client.disconnected
        assert assistant._pool.stats()["entries"] == 1

        await send(butler, "butler", "after")
        await send(assistant, "assistant", "after")
        assert scripted_factory.constructed == 3
        assert (
            scripted_factory.clients[2].options.mcp_servers["homeassistant"]
            is new_facade_config
        )
        assert not assistant_client.disconnected
    finally:
        await asyncio.gather(butler.aclose(), assistant.aclose())


async def test_options_fresh_vs_resumed_memory_blocks(agent_fixture,
                                                     scripted_factory):
    """Fresh connect carries <peer_overlay>; warm turns never rebuild options."""
    agent, send_turn = agent_fixture          # FakeSemanticMemory(overlay="OV")
    await send_turn("hello")
    opts0 = scripted_factory.clients[0].options
    assert "OV" in opts0.system_prompt
    await send_turn("again")
    assert scripted_factory.constructed == 1  # no second options build


async def test_warm_reuse_process_error_clears_sid_and_retries_fresh(
    agent_fixture, monkeypatch,
):
    """Finding 2 (final-review): a warm-reuse turn that dies with a
    non-retryable ProcessError must still hit the stale-resume fallback
    (clear sid + retry fresh) instead of surfacing raw to the user — even
    though the pool's ``_build`` callback (agent.py's only pre-fix source
    of ``last_resume["sid"]``) is skipped on warm reuse.

    ``FlakyClient`` raises on its SECOND ``query()`` call — i.e. turn 1 on a
    freshly-connected client succeeds and warms the entry; turn 2, a warm
    REUSE of that same client instance, is the one that blows up.
    """
    from claude_agent_sdk import ProcessError

    class FlakyClient(ScriptedClient):
        def __init__(self, options, sid: str) -> None:
            super().__init__(options, sid=sid)
            self.query_count = 0

        async def query(self, prompt, session_id="default"):
            self.query_count += 1
            if self.query_count == 2:
                # Non-retryable: exit code 1, message matches no retryable
                # pattern (not rate/timeout/overloaded; type name carries
                # none of CLI/SDK/Connection) -> classified UNKNOWN.
                raise ProcessError("boom", exit_code=1)
            await super().query(prompt, session_id=session_id)

    class FlakyFactory:
        def __init__(self) -> None:
            self.constructed = 0
            self.clients: list[FlakyClient] = []

        def __call__(self, options) -> FlakyClient:
            self.constructed += 1
            c = FlakyClient(options, sid=f"sid-attempt-{self.constructed}")
            self.clients.append(c)
            return c

    factory = FlakyFactory()
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)

    agent, send_turn = agent_fixture

    reply1 = await send_turn("hello")
    assert reply1 is not None and reply1.content.endswith("hello")
    channel_key = build_scoped_session_key("telegram", "assistant", "42")
    assert agent._session_registry.get(channel_key)["sdk_session_id"] == (
        "sid-attempt-1"
    )
    assert factory.constructed == 1

    # Turn 2: warm reuse of the SAME client -> its 2nd query() raises the
    # non-retryable ProcessError. Pre-fix, last_resume["sid"] was None here
    # (only _build, which warm reuse skips, ever set it) so the fallback's
    # `if last_resume["sid"] is None: raise` re-raised straight to the
    # caller. Post-fix, on_decision recorded "sid-attempt-1" under the entry
    # lock regardless of reuse, so the fallback clears it and retries fresh.
    reply2 = await send_turn("again")
    # did NOT surface the ProcessError to the caller:
    assert reply2 is not None and reply2.content.endswith("again")

    # The retry-fresh reconnect constructed a brand-new client and the
    # registry now holds ITS sid — proof the stale one was actually cleared
    # and replaced, not just silently kept.
    assert factory.constructed == 2
    assert agent._session_registry.get(channel_key)["sdk_session_id"] == (
        "sid-attempt-2"
    )


async def test_aclose_closes_pool_and_unsubscribes(agent_fixture,
                                                   scripted_factory):
    agent, send_turn = agent_fixture
    await send_turn("hello")
    await agent.aclose()
    assert scripted_factory.clients[0].disconnected
    await agent._session_registry.notify_reset("telegram-42")  # no boom


def test_claude_agent_options_fields_all_classified():
    """Fails when the SDK adds an options field nobody classified as
    static / connect-time / query-borne (spec §Q6)."""
    KNOWN = {
        # connect-time (pool-owned): every field agent._build_options sets
        "model", "system_prompt", "allowed_tools", "disallowed_tools",
        "permission_mode", "max_turns", "mcp_servers", "hooks", "cwd",
        "resume", "setting_sources", "plugins", "stderr",
        # voice partial-message streaming (2026-07-11 design §2 point 1):
        # connect-time, derived from `channel` — True only for voice.
        "include_partial_messages",
        # defaults Casa does not set — audited 2026-07-11 against SDK
        # 0.2.114 (spec §Q6); the 0.2.128 field set is identical, and the
        # `actual` assertion below re-checks it against the installed SDK
        # on every run: none is turn-variable in Casa's usage.
        "add_dirs", "agents", "betas", "can_use_tool", "cli_path",
        "continue_conversation", "debug_stderr", "effort",
        "enable_file_checkpointing", "env", "extra_args", "fallback_model",
        "fork_session", "include_hook_events",
        "load_timeout_ms", "max_budget_usd", "max_buffer_size",
        "max_thinking_tokens", "output_format", "permission_prompt_tool_name",
        "sandbox", "session_id", "session_store", "session_store_flush",
        "settings", "skills", "strict_mcp_config", "task_budget", "thinking",
        "tools", "user",
    }
    actual = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    assert actual <= KNOWN, (
        f"unclassified ClaudeAgentOptions fields {actual - KNOWN}: a new SDK "
        "field appeared — classify it static/connect-time/query-borne in the "
        "pooling spec §Q6 before extending this set"
    )


async def test_process_error_clear_declines_after_same_sid_reregistration(
    agent_fixture, monkeypatch,
):
    """#526: a concurrent turn that re-registered the SAME sid between this
    turn's resume decision and its ProcessError recovery moved the entry's
    registration GENERATION — the recovery's conditional clear must decline,
    and the retry must then RESUME that live session (pre-fix the sid-only
    guard matched, the entry was cleared, and the retry started fresh:
    continuity lost, and the cleared session was never consolidated)."""
    from claude_agent_sdk import ProcessError

    agent, send_turn = agent_fixture
    registry = agent._session_registry
    recorded_register: dict = {}
    real_register = registry.register

    async def recording_register(*args, **kwargs):
        recorded_register["args"] = args
        recorded_register["kwargs"] = kwargs
        await real_register(*args, **kwargs)

    monkeypatch.setattr(registry, "register", recording_register)

    class FlakyClient(ScriptedClient):
        def __init__(self, options, sid: str) -> None:
            super().__init__(options, sid=sid)
            self.query_count = 0

        async def query(self, prompt, session_id="default"):
            self.query_count += 1
            if self.query_count == 2:
                # Simulate T2: a concurrent same-key turn re-registers the
                # SAME sid (a successful resume of it) before T1's recovery
                # arm runs, then T1's attempt dies non-retryably.
                await real_register(
                    *recorded_register["args"], **recorded_register["kwargs"],
                )
                raise ProcessError("boom", exit_code=1)
            await super().query(prompt, session_id=session_id)

    class FlakyFactory:
        def __init__(self) -> None:
            self.constructed = 0
            self.clients: list[FlakyClient] = []

        def __call__(self, options) -> FlakyClient:
            self.constructed += 1
            c = FlakyClient(options, sid="sid-stable")
            self.clients.append(c)
            return c

    factory = FlakyFactory()
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)

    reply1 = await send_turn("hello")
    assert reply1 is not None and reply1.content.endswith("hello")
    channel_key = build_scoped_session_key("telegram", "assistant", "42")
    assert registry.get(channel_key)["sdk_session_id"] == "sid-stable"

    # Turn 2: warm reuse fails with ProcessError AFTER the same-sid
    # re-registration landed. The recovery clear must DECLINE.
    reply2 = await send_turn("again")
    assert reply2 is not None and reply2.content.endswith("again")

    # The retry saw the registry entry intact and RESUMED sid-stable —
    # pre-fix the clear emptied the entry and this reconnect was fresh
    # (options.resume None).
    assert factory.constructed == 2
    assert factory.clients[1].options.resume == "sid-stable"
    assert registry.get(channel_key)["sdk_session_id"] == "sid-stable"
