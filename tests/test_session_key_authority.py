"""#579: the session key, not the pool, is the serialization authority.

v0.66.0 made the SDK client pool own "who may run a turn on session key K".
The key outlives any pool instance, so every path that does not go through
*that* pool instance was uncovered. This file holds:

- **#579** — a reload builds a fresh Agent with its OWN pool while a turn is
  still running on the old one, so two clients could resume one session id.
  ``INV-CONC-003`` ("turns sharing a session key serialize under the pool
  entry's lock") is true within one pool; across a reload there are two.
- **replacement binding for INV-CONC-004** — #573 took the per-key write gate
  for SCHEDULED turns only and left ordinary turns to the pool entry lock.
  It is now unconditional, which is what makes the above reachable; the
  deleted ``_needs_session_gate`` classifier's test is replaced by
  ``test_an_ordinary_dm_turn_holds_the_gate`` here.

All of these drive ``Agent.handle_message`` through the same
``sdk_client_pool._default_make_client`` seam the pooled and bypass paths
share, so nothing here asserts against a stand-in for the mechanism itself.
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry, build_scoped_session_key
from session_reg_helpers import (
    RESIDENT_DIGEST,
    align_prompt_surface,
    resident_prov,
    resident_role_id,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


def _mk_text_block(text):
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text):
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]
        return m


def _mk_result(sid):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = False
    m.result = ""
    return m


class BlockingClient:
    """A client whose turn parks until released, so a second turn's attempt to
    start can be observed while the first is genuinely in flight."""

    def __init__(self, options, sid, *, started, release):
        self.options = options
        self._sid = sid
        self._started = started
        self._release = release
        self.queries = []
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        self._started.set()
        await self._release.wait()
        yield _mk_assistant("ok")
        yield _mk_result(self._sid)


def _make_config(role="assistant"):
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        role_id=resident_role_id(role),
        kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov(role),
    )


def _make_agent(registry):
    return Agent(
        config=_make_config(),
        session_registry=registry,
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _msg(text, chat_id="42"):
    return BusMessage(
        type=MessageType.REQUEST, source="user", target="assistant",
        content=text, channel="telegram", context={"chat_id": chat_id},
    )


class TestUnconditionalGate:
    async def test_an_ordinary_dm_turn_holds_the_gate(
        self, tmp_path, monkeypatch,
    ):
        """Replacement binding for INV-CONC-004 (#573's classifier is gone).

        Asserted directly on the module-level gate rather than through
        observable serialization, deliberately: within ONE pool the entry lock
        already orders same-key turns, so a "second turn cannot start"
        assertion passes just as well on the scheduled-only gate and pins
        nothing. What is new is that an ordinary DM REQUEST — the case #573
        excluded — takes the module-level gate, which is what makes the
        cross-reload (#579) guarantee reachable.
        """
        import session_gate

        started, release = asyncio.Event(), asyncio.Event()
        clients = []

        def factory(options):
            c = BlockingClient(
                options, f"sid-{len(clients) + 1}",
                started=started, release=release,
            )
            clients.append(c)
            return c

        monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
        registry = SessionRegistry(str(tmp_path / "sessions.json"))
        agent = _make_agent(registry)
        key = build_scoped_session_key("telegram", "assistant", "42")
        try:
            turn = asyncio.create_task(agent.handle_message(_msg("one")))
            await asyncio.wait_for(started.wait(), timeout=2)

            assert key in session_gate._SESSION_GATES, (
                "an ordinary DM turn did not take the per-key write gate"
            )

            release.set()
            await asyncio.wait_for(turn, timeout=5)
            # Refcounted: released once the turn ends.
            assert key not in session_gate._SESSION_GATES
        finally:
            release.set()
            await agent.aclose()


class TestReloadTwoPools:
    async def test_a_second_pool_cannot_resume_a_live_session(
        self, tmp_path, monkeypatch,
    ):
        """#579 red case.

        A reload constructs a fresh Agent — and therefore a fresh
        ``SdkClientPool`` — while a turn is still running on the old one, and
        drains the old pool in the BACKGROUND on purpose (a synchronous drain
        would deadlock, since casa_reload runs inside a warm client's turn).
        The replacement pool has no entry for the key, so it builds one cold:
        it reads the registry, sees the session id the running turn is using,
        and resumes it. Two clients, one session.

        The two pools' entry locks know nothing about each other, so only a
        module-level authority can order this. Fails before the gate became
        unconditional: the second agent connects a client while the first turn
        is still in flight.
        """
        started, release = asyncio.Event(), asyncio.Event()
        clients = []

        def factory(options):
            c = BlockingClient(
                options, f"sid-{len(clients) + 1}",
                started=started, release=release,
            )
            clients.append(c)
            return c

        monkeypatch.setattr("sdk_client_pool._default_make_client", factory)

        # ONE registry, as a reload keeps, PRE-SEEDED with a live pointer. The
        # seed is load-bearing: with an empty registry both turns would start
        # FRESH sessions, so an unfixed tree would show two clients but no
        # shared session id — proving concurrency, not the fork. What #579
        # actually costs is two clients resuming the SAME id, so the assertion
        # below is on `options.resume`, not on the client count alone.
        registry = SessionRegistry(str(tmp_path / "sessions.json"))
        key = build_scoped_session_key("telegram", "assistant", "42")
        await registry.register(
            channel_key=key, agent=resident_role_id("assistant"),
            sdk_session_id="sid-live", binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("assistant"),
            user_provenance=resident_prov("assistant"),
        )
        old_agent = _make_agent(registry)
        new_agent = _make_agent(registry)      # what _construct_agent builds
        # #1029: the seed has to speak for a structural prompt surface too, or
        # the surface gate retires it and BOTH turns start fresh — which would
        # quietly un-set-up this test exactly as an empty registry would.
        align_prompt_surface(registry, old_agent, "telegram", "42")
        try:
            in_flight = asyncio.create_task(old_agent.handle_message(_msg("one")))
            await asyncio.wait_for(started.wait(), timeout=2)
            assert clients[0].options.resume == "sid-live", (
                "the first turn did not resume the live pointer, so this test "
                "is not set up to observe a fork"
            )

            # The reload lands; a new message for the SAME key is handled by
            # the replacement agent while the old turn is still running.
            after_reload = asyncio.create_task(
                new_agent.handle_message(_msg("two")),
            )
            await asyncio.sleep(0.05)
            # Snapshotted, not asserted here: the count is the MECHANISM, and
            # asserting it mid-flight would short-circuit before the outcome
            # assertion below ever runs. On a broken tree the fork is what
            # costs turns, so the fork is what fails first.
            concurrent_clients = len(clients)

            release.set()
            await asyncio.wait_for(
                asyncio.gather(in_flight, after_reload), timeout=5,
            )

            # THE fork assertion: no two clients ever resumed one session id.
            resumed = [c.options.resume for c in clients if c.options.resume]
            assert len(resumed) == len(set(resumed)), (
                f"two clients resumed the same session id: {resumed}"
            )
            assert concurrent_clients == 1, (
                "the replacement pool started a second client for a session "
                "key whose turn is still in flight on the old pool"
            )
            # And they ran in sequence: the second resumed what the first
            # published, rather than the pointer the first started from.
            assert clients[1].options.resume == clients[0]._sid, (
                "the second turn did not resume the first turn's published "
                "session, so they did not serialize"
            )
            entry = registry.get(key)
            assert entry is not None
            assert entry["sdk_session_id"] == clients[1]._sid
        finally:
            release.set()
            await old_agent.aclose()
            await new_agent.aclose()
