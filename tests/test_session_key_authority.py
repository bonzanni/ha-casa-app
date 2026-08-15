"""#578/#579: the session key, not the pool, is the serialization authority.

v0.66.0 made the SDK client pool own "who may run a turn on session key K".
The key outlives any pool instance, so every path that does not go through
*that* pool instance was uncovered. Two reachable defects followed, and this
file holds their red cases:

- **#579** — a reload builds a fresh Agent with its OWN pool while a turn is
  still running on the old one, so two clients could resume one session id.
  ``INV-CONC-003`` ("turns sharing a session key serialize under the pool
  entry's lock") is true within one pool; across a reload there are two.
- **#578** — a memory wipe joined in-flight turns through the pool's reset
  listener, so a turn the pool never owned (a SCHEDULED turn, a webhook
  one-shot, a ``PoolUnavailable`` fallback) was not joined and re-armed the
  session pointer after the wipe reported completion. A first-ever turn on a
  key is worse still: it has no registry entry for the wipe to enumerate.
- **replacement binding for INV-CONC-004** — #573 took the per-key write gate
  for SCHEDULED turns only and left ordinary turns to the pool entry lock.
  It is now unconditional, which is what makes the above reachable; the
  deleted ``_needs_session_gate`` classifier's test is replaced by
  ``test_an_ordinary_dm_turn_holds_the_gate_and_admission`` here.

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
from session_reg_helpers import RESIDENT_DIGEST, resident_prov, resident_role_id

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

try:
    from tests.test_agent_pooling import FakeSemanticMemory
except ImportError:
    from test_agent_pooling import FakeSemanticMemory


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
        semantic_memory=FakeSemanticMemory(overlay="OV"),
    )


def _msg(text, chat_id="42"):
    return BusMessage(
        type=MessageType.REQUEST, source="user", target="assistant",
        content=text, channel="telegram", context={"chat_id": chat_id},
    )


class TestUnconditionalGate:
    async def test_an_ordinary_dm_turn_holds_the_gate_and_admission(
        self, tmp_path, monkeypatch, fresh_admission,
    ):
        """Replacement binding for INV-CONC-004 (#573's classifier is gone).

        Asserted directly on the two authorities rather than through observable
        serialization, deliberately: within ONE pool the entry lock already
        orders same-key turns, so a "second turn cannot start" assertion passes
        just as well on the scheduled-only gate and pins nothing. What is new
        is that an ordinary DM REQUEST — the case #573 excluded — is itself a
        participant in both module-level authorities, which is what makes the
        cross-reload (#579) and wipe-drain (#578) guarantees reachable.
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
            assert fresh_admission._barrier._active_shared > 0, (
                "an ordinary DM turn was not admitted, so a wipe would not "
                "drain it"
            )

            release.set()
            await asyncio.wait_for(turn, timeout=5)
            # Refcounted: both are released once the turn ends.
            assert key not in session_gate._SESSION_GATES
            assert fresh_admission._barrier._active_shared == 0
        finally:
            release.set()
            await agent.aclose()


class WipeableMemory(FakeSemanticMemory):
    def __init__(self):
        super().__init__(overlay="OV")
        self.deleted = []

    async def delete_bank(self, bank):
        self.deleted.append(bank)
        return True


@pytest.fixture
def fresh_admission(monkeypatch):
    """A per-test TurnAdmission, substituted for the process singleton.

    Both the turn side (``agent._turn_admission()``) and the wipe side read
    the module attribute at call time, so they share whatever this installs.
    Needed because the singleton's ``asyncio.Event`` binds to the first loop
    that awaits it and pytest-asyncio gives each test a fresh loop — the same
    reason the wipe suite constructs its own ``RetainFence`` rather than using
    ``memory_wipe.FENCE``.
    """
    import session_gate

    adm = session_gate.TurnAdmission()
    monkeypatch.setattr(session_gate, "TURN_ADMISSION", adm)
    return adm


@pytest.fixture
def fresh_fence(monkeypatch):
    """A per-test RetainFence substituted for ``memory_wipe.FENCE``.

    Load-bearing for the deadlock tests, not hygiene: ``save_session`` reaches
    the fence through the module attribute, so a wipe handed its own private
    ``RetainFence()`` shares NO fence with a concurrent reset and cannot
    reproduce a gate/fence inversion at all. Substituting the module attribute
    and handing the wipe the same instance is what makes the two sides
    actually contend.
    """
    import memory_wipe as mw

    fence = mw.RetainFence()
    monkeypatch.setattr(mw, "FENCE", fence)
    return fence


async def _run_wipe(registry, sem, fence):
    import memory_wipe as mw

    return await mw.wipe_long_term_memory(
        registry=registry, semantic_memory=sem, fence=fence,
        bank="casa", retry_dir="/nonexistent-spool-for-test",
    )


def _scheduled_msg(text, chat_id="42"):
    """A SCHEDULED turn takes the BYPASS path (AR-6): it owns a one-shot
    ManagedSdkClient the pool has never heard of, which is exactly the turn
    the pool's reset listener could not join."""
    return BusMessage(
        type=MessageType.SCHEDULED, source="scheduler", target="assistant",
        content=text, channel="telegram", context={"chat_id": chat_id},
    )


class TestWipeDrainsEveryPath:
    """#578 red cases. ``notify_reset`` fans out to exactly one listener in the
    whole codebase — the pool's ``close_key`` — so a turn the pool never owned
    was never joined, and the wipe's retirement claims are released in its
    ``finally`` before that turn publishes."""

    async def test_a_bypass_turn_cannot_rearm_the_pointer(
        self, tmp_path, monkeypatch, fresh_admission,
    ):
        """#578 as filed: the pointer is back, naming a session whose CLI
        transcript still holds the pre-wipe conversation, after a report that
        said the wipe completed."""
        import memory_wipe as mw

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
        key = build_scoped_session_key("telegram", "assistant", "42")
        await registry.register(
            channel_key=key, agent=resident_role_id("assistant"),
            sdk_session_id="sid-pre-wipe",
            binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("assistant"),
            user_provenance=resident_prov("assistant"),
        )
        agent = _make_agent(registry)
        sem = WipeableMemory()
        try:
            turn = asyncio.create_task(
                agent.handle_message(_scheduled_msg("fire")),
            )
            await asyncio.wait_for(started.wait(), timeout=2)

            wipe = asyncio.create_task(_run_wipe(registry, sem, mw.RetainFence()))
            await asyncio.sleep(0.05)
            assert not wipe.done(), (
                "the wipe completed while a bypass turn was still in flight"
            )

            release.set()
            await asyncio.wait_for(turn, timeout=5)
            await asyncio.wait_for(wipe, timeout=5)

            assert registry.get(key) is None, (
                "a bypass turn re-armed the session pointer after the wipe "
                "reported completion"
            )
            assert sem.deleted == ["casa"]
        finally:
            release.set()
            await agent.aclose()

    async def test_a_first_ever_turn_cannot_survive_the_wipe(
        self, tmp_path, monkeypatch, fresh_admission,
    ):
        """Sol design-r1: gating the keys in ``registry.all_entries()`` cannot
        cover a FIRST-EVER turn on a key — it is running with no registry entry
        to enumerate, so the wipe gates nothing, claims nothing, deletes the
        bank, reports success, and the turn then publishes a pointer to a
        transcript that predates the wipe. Only closing turn admission before
        discovering keys covers it."""
        import memory_wipe as mw

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
        # EMPTY registry: nothing for the wipe to enumerate.
        registry = SessionRegistry(str(tmp_path / "sessions.json"))
        key = build_scoped_session_key("telegram", "assistant", "42")
        assert registry.get(key) is None
        agent = _make_agent(registry)
        sem = WipeableMemory()
        try:
            turn = asyncio.create_task(
                agent.handle_message(_scheduled_msg("first ever")),
            )
            await asyncio.wait_for(started.wait(), timeout=2)

            wipe = asyncio.create_task(_run_wipe(registry, sem, mw.RetainFence()))
            await asyncio.sleep(0.05)
            assert not wipe.done(), (
                "the wipe completed while a first-ever turn was in flight"
            )

            release.set()
            await asyncio.wait_for(turn, timeout=5)
            await asyncio.wait_for(wipe, timeout=5)

            assert registry.get(key) is None, (
                "a turn that started before the wipe, on a key with no entry "
                "to enumerate, left a resumable pointer behind it"
            )
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


class TestResetVersusWipe:
    """The deadlock that killed design r1, pinned so it cannot come back.

    r1 had the wipe take the RetainFence exclusively and THEN a per-key session
    gate, while ``/new`` took the gate and then entered the fence's shared side
    through ``save_session``. AB/BA — a permanent resident deadlock, found
    independently by both design reviewers. The mandated order is now
    ``TurnAdmission -> session_write_gate -> RetainFence -> pool entry lock``.

    What each test below actually pins, stated precisely because the
    distinction was a diff-review finding:

    - The two completion tests pin that the shipped COMBINATION cannot
      deadlock, in both orders, on a bounded timeout rather than a hang.
      Verified by reconstructing r1 wholesale (``/new`` exempt from admission
      AND the wipe acquiring a per-key gate inside the fence), which makes both
      fail. They do NOT isolate the ordering: with admission held, a wipe and a
      reset never overlap, so re-adding the gate acquisition ALONE would leave
      them green — there would be no deadlock left for them to catch.
    - ``test_the_wipe_takes_no_session_gate`` is what pins the ordering
      property itself: the wipe holds no per-key gate, which is what removes
      the cycle at its root rather than ordering around it.
    """

    async def test_the_wipe_takes_no_session_gate(
        self, tmp_path, fresh_admission, fresh_fence, monkeypatch,
    ):
        """The root-cause property, pinned directly.

        r1's deadlock existed because the wipe waited for a per-key gate while
        holding the fence. The shipped design does not order that acquisition
        more carefully — it removes it: turn admission has already drained
        every turn, so there is nothing left for a per-key gate to exclude.
        A wipe that starts acquiring session gates again has reintroduced the
        AB/BA hazard even if no test deadlocks that day.
        """
        import session_gate

        acquired = []

        class RecordingGates(dict):
            """Instruments the shared ``_SESSION_GATES`` mapping rather than
            the ``session_write_gate`` callable.

            Deliberate: patching the callable only catches callers that look
            it up on the module at call time. A ``from session_gate import
            session_write_gate`` alias bound at import time keeps the original
            function and would slip past unseen — which is exactly how a
            reintroduction is most likely to be written. Every alias, however
            bound, reaches this one dict.
            """

            def get(self, key, default=None):
                acquired.append(key)
                return super().get(key, default)

            def __setitem__(self, key, value):
                acquired.append(key)
                super().__setitem__(key, value)

        monkeypatch.setattr(
            session_gate, "_SESSION_GATES", RecordingGates(),
        )

        registry = SessionRegistry(str(tmp_path / "sessions.json"))
        for scope in ("42", "43"):
            await registry.register(
                channel_key=f"telegram-{scope}",
                agent=resident_role_id("assistant"),
                sdk_session_id=f"sid-{scope}", binding_digest=RESIDENT_DIGEST,
                speaker_provenance=resident_prov("assistant"),
                user_provenance=resident_prov("assistant"),
            )
        report = await _run_wipe(registry, WipeableMemory(), fresh_fence)

        assert report.session_entries_dropped == 2, (
            "the wipe did not do its work, so 'took no gate' proves nothing"
        )
        assert acquired == [], (
            f"the wipe acquired session gate(s) {acquired} — it holds the "
            "RetainFence while it runs, so waiting for a per-key gate "
            "reintroduces the r1 AB/BA deadlock against /new"
        )

    async def test_a_wipe_and_a_reset_do_not_deadlock(
        self, tmp_path, fresh_admission, fresh_fence, monkeypatch,
    ):
        import session_saver
        from session_saver import reset_channel

        registry = SessionRegistry(str(tmp_path / "sessions.json"))
        key = "telegram-42"
        await registry.register(
            channel_key=key, agent=resident_role_id("assistant"),
            sdk_session_id="sid-old", binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("assistant"),
            user_provenance=resident_prov("assistant"),
        )
        sem = WipeableMemory()

        async def fake_classify(content):
            return "public"

        monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
        msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]

        with monkeypatch.context() as m:
            m.setattr(
                "session_saver.get_session_messages", lambda *a, **k: msgs,
            )
            reset = asyncio.create_task(
                reset_channel(key, registry, sem, channel="telegram"),
            )
            wipe = asyncio.create_task(
                _run_wipe(registry, sem, fresh_fence),
            )
            await asyncio.wait_for(
                asyncio.gather(reset, wipe), timeout=5,
            )

        assert registry.get(key) is None

    async def test_a_reset_started_first_still_completes(
        self, tmp_path, fresh_admission, fresh_fence, monkeypatch,
    ):
        """The other order: the reset is already inside its admission when the
        wipe asks for exclusivity."""
        import session_saver
        from session_saver import reset_channel

        registry = SessionRegistry(str(tmp_path / "sessions.json"))
        key = "telegram-42"
        await registry.register(
            channel_key=key, agent=resident_role_id("assistant"),
            sdk_session_id="sid-old", binding_digest=RESIDENT_DIGEST,
            speaker_provenance=resident_prov("assistant"),
            user_provenance=resident_prov("assistant"),
        )
        sem = WipeableMemory()
        reset_reached_close = asyncio.Event()

        async def slow_listener(_key):
            reset_reached_close.set()
            await asyncio.sleep(0.05)

        registry.add_reset_listener(slow_listener)

        async def fake_classify(content):
            return "public"

        monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
        msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]

        with monkeypatch.context() as m:
            m.setattr(
                "session_saver.get_session_messages", lambda *a, **k: msgs,
            )
            reset = asyncio.create_task(
                reset_channel(key, registry, sem, channel="telegram"),
            )
            await asyncio.wait_for(reset_reached_close.wait(), timeout=2)
            wipe = asyncio.create_task(
                _run_wipe(registry, sem, fresh_fence),
            )
            await asyncio.wait_for(asyncio.gather(reset, wipe), timeout=5)

        assert registry.get(key) is None
