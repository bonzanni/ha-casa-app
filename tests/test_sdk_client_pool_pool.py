"""SdkClientPool tests. Uses ScriptedClient + factories from
test_sdk_client_pool_client (copy the helpers — no cross-test imports)."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


# Contextvars for testing
test_origin: ContextVar = ContextVar("test_origin", default=None)
test_cid: ContextVar = ContextVar("test_cid", default="-")
test_engagement: ContextVar = ContextVar("test_engagement", default=None)


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


def _mk_result(sid, usage=None, *, is_error=False, result=""):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = is_error
    m.result = result
    if usage is not None:
        m.usage = usage
    return m


async def _async_result(value):
    return value


class ScriptedClient:
    def __init__(self, options):
        self.options = options
        self.connected = False
        self.disconnected = False
        self.interrupts = 0
        self.queries: list[str] = []
        self.script: list[list] = []      # one list of messages per query()
        self._buffer: list = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        if self.script:
            self._buffer.extend(self.script.pop(0))

    async def interrupt(self):
        self.interrupts += 1

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage
        while self._buffer:
            msg = self._buffer.pop(0)
            yield msg
            if isinstance(msg, ResultMessage):
                return


class FakeRegistry:
    def __init__(self):
        self.data = {}
        self.touched = []
        self.generations = {}
    def get(self, key):
        return self.data.get(key)
    def generation(self, key):
        return self.generations.get(key)
    async def touch(self, key):
        self.touched.append(key)


from agent import ResumeDecision, SessionEntrySnapshot


def _snap(entry):
    """Build a SessionEntrySnapshot from a test reg entry (which may omit the
    'agent' field the production snapshot_session_entry requires)."""
    if entry is None:
        return None
    return SessionEntrySnapshot(
        agent=entry.get("agent", "resident:test"),
        sdk_session_id=entry.get("sdk_session_id", ""),
        last_active=entry.get("last_active"),
        scope_class=entry.get("scope_class"),
        binding_digest=entry.get("binding_digest"),
        speaker_provenance=None, user_provenance=None,
    )


def _resume_dec(entry):
    return ResumeDecision(
        "resume", entry["sdk_session_id"], False, _snap(entry), "fresh",
    )


def _new_dec(entry=None, *, retain_old=False):
    return ResumeDecision(
        "new", None, retain_old,
        _snap(entry) if retain_old else None,
        "expired" if retain_old else "missing",
    )


def _decide_resume(channel, entry, now):
    return _resume_dec(entry) if entry and entry.get("sdk_session_id") else _new_dec()


def _mk_pool(registry, *, decide=_decide_resume, **kw):
    from sdk_client_pool import SdkClientPool
    return SdkClientPool(
        registry, decide=decide,
        origin_ctxvar=test_origin, cid_ctxvar=test_cid,
        engagement_ctxvar=test_engagement,
        make_client=ScriptedClient, **kw,
    )


async def test_cold_hit_connects_with_resume_and_touches():
    reg = FakeRegistry()
    reg.data["voice-s1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    # Preload the script through make_client capture:
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid):
        return {"resume": resume_sid}
    async def on_message(m): pass

    async def go():
        return await pool.turn(
            channel_key="voice-s1", channel="voice", prompt="hi",
            origin={}, cid="c", build_options=build_options,
            on_stale_old=lambda s, g=None: None, on_message=on_message)

    t = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")]]
    res = await t
    assert res.resume_sid == "sid-0" and res.is_fresh is False
    assert reg.touched == ["voice-s1"]
    assert made[0].options == {"resume": "sid-0"}


async def test_cold_connect_logs_monotonic_elapsed_ms(caplog):
    import logging

    now = [10.0]
    reg = FakeRegistry()
    pool = _mk_pool(reg, monotonic=lambda: now[0])

    class TimedConnectClient(ScriptedClient):
        async def connect(self):
            await super().connect()
            now[0] = 10.125

    pool._make_client = TimedConnectClient

    async def build_options(is_fresh, resume_sid):
        return {}

    async def on_message(_message):
        return None

    with caplog.at_level(logging.INFO, logger="sdk_client_pool"):
        await pool.turn(
            channel_key="voice-latency",
            channel="voice",
            prompt="secret prompt",
            origin={},
            cid="c",
            build_options=build_options,
            on_stale_old=lambda _sid, _g=None: None,
            on_message=on_message,
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sdk_client_pool" and "pool cold connect" in record.getMessage()
    ]
    assert messages == [
        "pool cold connect key=voice-latency resume=False ms=125"
    ]
    assert "secret prompt" not in caplog.text


async def test_session_publish_logs_monotonic_elapsed_ms(caplog):
    import logging

    now = [10.0]
    pool = _mk_pool(FakeRegistry(), monotonic=lambda: now[0])
    made = []

    def make_client(options):
        client = ScriptedClient(options)
        client.script = [[_mk_result("secret-session-id")]]
        made.append(client)
        return client

    pool._make_client = make_client
    published = []

    async def on_success(sid):
        published.append(sid)
        now[0] = 10.125

    try:
        with caplog.at_level(logging.INFO, logger="sdk_client_pool"):
            await pool.turn(
                channel_key="voice-publish-latency",
                channel="voice",
                prompt="secret publish prompt",
                origin={},
                cid="c",
                build_options=lambda _fresh, _resume: _async_result({}),
                on_stale_old=lambda _sid, _g=None: None,
                on_message=lambda _message: _async_result(None),
                on_success=on_success,
            )
    finally:
        await pool.aclose()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sdk_client_pool"
        and "pool session publish" in record.getMessage()
    ]
    assert messages == ["pool session publish ok=True ms=125"]
    assert published == ["secret-session-id"]
    assert made[0].disconnected
    assert "secret-session-id" not in caplog.text
    assert "secret publish prompt" not in caplog.text


async def test_session_publish_failure_logs_sanitized_elapsed_and_drops(caplog):
    import logging

    class PublishFailure(Exception):
        pass

    now = [20.0]
    pool = _mk_pool(FakeRegistry(), monotonic=lambda: now[0])
    made = []

    def make_client(options):
        client = ScriptedClient(options)
        client.script = [[_mk_result("secret-failed-session-id")]]
        made.append(client)
        return client

    pool._make_client = make_client
    callbacks = 0

    async def on_success(_sid):
        nonlocal callbacks
        callbacks += 1
        now[0] = 20.25
        raise PublishFailure("secret publication failure")

    try:
        with caplog.at_level(logging.INFO, logger="sdk_client_pool"):
            with pytest.raises(PublishFailure, match="secret publication failure"):
                await pool.turn(
                    channel_key="voice-publish-failure",
                    channel="voice",
                    prompt="secret failure prompt",
                    origin={},
                    cid="c",
                    build_options=lambda _fresh, _resume: _async_result({}),
                    on_stale_old=lambda _sid, _g=None: None,
                    on_message=lambda _message: _async_result(None),
                    on_success=on_success,
                )
        assert pool.stats()["entries"] == 0
        assert made[0].disconnected
    finally:
        await pool.aclose()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sdk_client_pool"
        and "pool session publish" in record.getMessage()
    ]
    assert messages == ["pool session publish ok=False ms=250"]
    assert callbacks == 1
    assert "secret-failed-session-id" not in caplog.text
    assert "secret failure prompt" not in caplog.text
    assert "secret publication failure" not in caplog.text


async def test_cancelled_session_publish_logs_elapsed_and_drops(caplog):
    import logging

    now = [30.0]
    pool = _mk_pool(FakeRegistry(), monotonic=lambda: now[0])
    made = []

    def make_client(options):
        client = ScriptedClient(options)
        client.script = [[_mk_result("secret-cancelled-session-id")]]
        made.append(client)
        return client

    pool._make_client = make_client
    publish_started = asyncio.Event()
    never_publish = asyncio.Event()
    callbacks = 0

    async def on_success(_sid):
        nonlocal callbacks
        callbacks += 1
        publish_started.set()
        await never_publish.wait()

    task = None
    try:
        with caplog.at_level(logging.INFO, logger="sdk_client_pool"):
            task = asyncio.create_task(pool.turn(
                channel_key="voice-publish-cancelled",
                channel="voice",
                prompt="secret cancelled prompt",
                origin={},
                cid="c",
                build_options=lambda _fresh, _resume: _async_result({}),
                on_stale_old=lambda _sid, _g=None: None,
                on_message=lambda _message: _async_result(None),
                on_success=on_success,
            ))
            await asyncio.wait_for(publish_started.wait(), timeout=1)
            now[0] = 30.5
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert pool.stats()["entries"] == 0
        assert made[0].disconnected
    finally:
        never_publish.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await pool.aclose()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sdk_client_pool"
        and "pool session publish" in record.getMessage()
    ]
    assert messages == ["pool session publish ok=False ms=500"]
    assert callbacks == 1
    assert "secret-cancelled-session-id" not in caplog.text
    assert "secret cancelled prompt" not in caplog.text


async def test_warm_reuse_skips_connect_and_build_options():
    reg = FakeRegistry()
    reg.data["voice-s1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    builds = []
    async def build_options(is_fresh, resume_sid):
        builds.append((is_fresh, resume_sid))
        return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="voice-s1", channel="voice",
                               prompt="hi", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s, g=None: None,
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")]]
    await t
    # Turn 2 — warm: same client object, no new construction, no build_options
    made[0].script = [[_mk_result("sid-0")]]
    await go()
    assert len(made) == 1
    assert builds == [(False, "sid-0")]     # only the cold connect built options


async def test_on_decision_fires_on_cold_connect_and_warm_reuse():
    """Finding 2 (final-review): on_decision(resume_sid, is_fresh) must fire
    for EVERY turn, under the entry lock, right after the decision is
    derived — including a warm-reuse turn that skips build_options
    entirely. Without this, a caller tracking "last resume sid" only from
    build_options loses visibility into warm-reuse turns."""
    reg = FakeRegistry()
    reg.data["voice-s1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    # #526: on_decision also carries the entry's registration generation,
    # captured in the same under-lock block as the decision's snapshot.
    reg.generations["voice-s1"] = 7
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    decisions = []
    def on_decision(resume_sid, is_fresh, generation):
        decisions.append((resume_sid, is_fresh, generation))
    async def go():
        return await pool.turn(channel_key="voice-s1", channel="voice",
                               prompt="hi", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s, g=None: None,
                               on_message=on_message,
                               on_decision=on_decision)
    # Turn 1 — cold connect (resume sid-0 from the registry).
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")]]
    await t
    assert decisions == [("sid-0", False, 7)]
    # Turn 2 — warm reuse: same client, no new construction, but on_decision
    # must still fire with the same resume sid.
    made[0].script = [[_mk_result("sid-0")]]
    await go()
    assert len(made) == 1                       # confirms this WAS a warm reuse
    assert decisions == [("sid-0", False, 7), ("sid-0", False, 7)]


async def test_decision_new_closes_old_awaits_disconnect_then_stale_cb():
    """AR-3 + AR-4 ordering: old entry fully disconnected BEFORE
    on_stale_old fires (cold-retain reads the flushed transcript)."""
    reg = FakeRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-old", "last_active": "x"}
    order = []
    def decide(channel, entry, now):
        return _resume_dec(entry) if not order else _new_dec(entry, retain_old=True)
    pool = _mk_pool(reg, decide=decide)
    made = []
    def mk(opts):
        c = ScriptedClient(opts)
        real = c.disconnect
        async def d():
            order.append("disconnect")
            await real()
        c.disconnect = d
        made.append(c); return c
    pool._make_client = mk
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="tg-1", channel="telegram",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s, g=None: order.append(f"stale:{s.sdk_session_id}"),
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-old")]]
    await t
    order.append("turn2")
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[1].script = [[_mk_result("sid-new")]]
    res = await t
    assert res.is_fresh is True
    after_turn2 = order[order.index("turn2") + 1:]
    assert after_turn2[:2] == ["disconnect", "stale:sid-old"]  # AR-4 ordering


async def test_sid_mismatch_reconnects_on_registry_sid():
    """Pins INV-TURN-001. Red case demonstrated: dropping the entry.sid == resume_sid half of the reusable check fails this test."""
    reg = FakeRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-A", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {"resume": resume_sid}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="tg-1", channel="telegram",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s, g=None: None,
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-A")]]
    await t
    # External rewrite (another path registered a different sid):
    reg.data["tg-1"]["sdk_session_id"] = "sid-B"
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[1].script = [[_mk_result("sid-B")]]
    await t
    assert len(made) == 2
    assert made[1].options == {"resume": "sid-B"}
    assert made[0].disconnected


async def test_turn_failure_invalidates_entry_so_next_attempt_reconnects():
    # NOTE (stabilization, not an assertion change): ScriptedClient.connect()/
    # query() have no real internal awaits, so a plain `asyncio.sleep(0.01)`
    # does not reliably pause the task *between* connect completing and
    # query() being invoked — the whole cold-connect-and-turn sequence can
    # run to completion inside that sleep with nothing left to patch. Use an
    # explicit Event, set right after connect(), so the test deterministically
    # gets control back before pool.turn() calls query() on the new client.
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    connected = asyncio.Event()

    def mk(opts):
        c = ScriptedClient(opts)
        real_connect = c.connect

        async def connect():
            await real_connect()
            connected.set()
        c.connect = connect
        made.append(c)
        return c
    pool._make_client = mk
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s, g=None: None, on_message=on_message)
    t = asyncio.create_task(go())
    await connected.wait()

    class Boom(Exception): pass
    async def bad_query(prompt, session_id="default"): raise Boom()
    made[0].query = bad_query
    with pytest.raises(Boom):
        await t
    assert pool.stats()["entries"] == 0     # invalidated + dropped
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[1].script = [[_mk_result("sid-0")]]
    await t                                  # attempt 2 reconnected fine


async def test_concurrent_first_turns_single_connect():
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s, g=None: None, on_message=on_message)
    t1 = asyncio.create_task(go())
    t2 = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")], [_mk_result("sid-0")]]
    await asyncio.gather(t1, t2)
    assert len(made) == 1                    # one client served both, serialized


async def test_invalidation_serializes_same_key_until_active_turn_releases():
    """A cleared generation remains a same-key handoff barrier until its
    active turn releases the entry lock, but neither unrelated keys nor the
    replacement generation wait for the old transport to finish closing.
    

    Pins INV-CONC-003 (with the single-connect sibling). Red case demonstrated: a turn-path mutation that breaks the locked decision (reuse regardless of sid) fails these tests.
    """
    reg = FakeRegistry()
    reg.data["voice-same"] = {
        "sdk_session_id": "sid-same", "last_active": "x",
    }
    reg.data["voice-other"] = {
        "sdk_session_id": "sid-other", "last_active": "x",
    }
    pool = _mk_pool(reg)
    first_turn_started = asyncio.Event()
    release_first_turn = asyncio.Event()
    old_disconnect_started = asyncio.Event()
    release_old_disconnect = asyncio.Event()
    made = []
    same_key_generations = 0

    class GatedClient(ScriptedClient):
        def __init__(self, options, *, same_generation):
            super().__init__(options)
            self.same_generation = same_generation

        async def query(self, prompt, session_id="default"):
            self.queries.append(prompt)
            if prompt == "first":
                first_turn_started.set()

        async def receive_response(self):
            prompt = self.queries[-1]
            if prompt == "first":
                await release_first_turn.wait()
            sid = (
                "sid-other"
                if self.options["key"] == "voice-other"
                else "sid-same"
            )
            yield _mk_result(sid)

        async def disconnect(self):
            self.disconnected = True
            if self.same_generation == 1:
                old_disconnect_started.set()
                await release_old_disconnect.wait()

    def make_client(options):
        nonlocal same_key_generations
        generation = None
        if options["key"] == "voice-same":
            same_key_generations += 1
            generation = same_key_generations
        client = GatedClient(options, same_generation=generation)
        made.append(client)
        return client

    pool._make_client = make_client

    async def go(key, prompt):
        async def build_options(is_fresh, resume_sid):
            return {"key": key, "resume": resume_sid}

        async def on_message(_message):
            return None

        return await pool.turn(
            channel_key=key,
            channel="voice",
            prompt=prompt,
            origin={},
            cid="c",
            build_options=build_options,
            on_stale_old=lambda _sid, _g=None: None,
            on_message=on_message,
        )

    first = asyncio.create_task(go("voice-same", "first"))
    invalidation = None
    replacement = None
    try:
        await asyncio.wait_for(first_turn_started.wait(), timeout=1)
        invalidation = asyncio.create_task(pool.invalidate_all())
        replacement = asyncio.create_task(go("voice-same", "second"))

        # Let invalidation remove the first generation and block on its
        # actual in-turn lock. The replacement must not even construct yet.
        await asyncio.sleep(0)
        assert same_key_generations == 1

        # The barrier is per key, not global.
        other = await asyncio.wait_for(
            go("voice-other", "other"), timeout=1,
        )
        assert other.sid == "sid-other"

        # Ending the old turn transfers ownership to invalidation. It drops
        # the handoff barrier before awaiting the old transport close.
        release_first_turn.set()
        assert (await asyncio.wait_for(first, timeout=1)).sid == "sid-same"
        await asyncio.wait_for(old_disconnect_started.wait(), timeout=1)
        assert not invalidation.done()

        result = await asyncio.wait_for(replacement, timeout=1)
        assert result.sid == "sid-same"
        assert same_key_generations == 2
        assert not invalidation.done()

        release_old_disconnect.set()
        await asyncio.wait_for(invalidation, timeout=1)
    finally:
        release_first_turn.set()
        release_old_disconnect.set()
        tasks = [
            task for task in (first, invalidation, replacement)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await pool.aclose()


async def test_cancelled_invalidation_keeps_active_generation_barrier():
    """Caller cancellation and a repeated invalidation cannot release the
    active generation's barrier before its entry lock actually drains.
    """
    pool = _mk_pool(FakeRegistry())
    old = await pool._entry_stub("voice-same")
    await old.lock.acquire()
    invalidation = asyncio.create_task(pool.invalidate_all())
    replacement = None
    try:
        # The first invalidation snapshots the entry and owns its barrier.
        while "voice-same" in pool._entries:
            await asyncio.sleep(0)

        # A concurrent/repeated invalidation sees no current generation. It
        # must neither replace nor release the first invalidation's barrier.
        await pool.invalidate_all()
        replacement = asyncio.create_task(pool._entry_stub("voice-same"))
        await asyncio.sleep(0)
        assert not replacement.done()

        # Cancelling the caller may stop its wait, but the lock-handoff close
        # must continue in the background so a new client cannot overlap the
        # still-active old generation.
        invalidation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invalidation
        await asyncio.sleep(0)
        assert not replacement.done()

        old.lock.release()
        current = await asyncio.wait_for(replacement, timeout=1)
        assert current is not old
        for _ in range(10):
            if old.state == "closed":
                break
            await asyncio.sleep(0)
        assert old.state == "closed"
    finally:
        if old.lock.locked():
            old.lock.release()
        if replacement is not None and not replacement.done():
            replacement.cancel()
        await asyncio.gather(
            *(task for task in (invalidation, replacement) if task is not None),
            return_exceptions=True,
        )
        await pool.aclose()


async def test_cancelled_waiter_does_not_cancel_shared_invalidation_barrier():
    pool = _mk_pool(FakeRegistry())
    old = await pool._entry_stub("voice-same")
    await old.lock.acquire()
    invalidation = asyncio.create_task(pool.invalidate_all())
    first_waiter = None
    second_waiter = None
    try:
        while "voice-same" in pool._entries:
            await asyncio.sleep(0)
        first_waiter = asyncio.create_task(pool._entry_stub("voice-same"))
        second_waiter = asyncio.create_task(pool._entry_stub("voice-same"))
        await asyncio.sleep(0)

        first_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_waiter
        await asyncio.sleep(0)
        assert not second_waiter.done()

        old.lock.release()
        current = await asyncio.wait_for(second_waiter, timeout=1)
        assert current is not old
        await asyncio.wait_for(invalidation, timeout=1)
    finally:
        if old.lock.locked():
            old.lock.release()
        tasks = [
            task for task in (invalidation, first_waiter, second_waiter)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await pool.aclose()


async def test_pool_close_wakes_barrier_waiters_without_reopening_generation():
    from sdk_client_pool import PoolUnavailable

    pool = _mk_pool(FakeRegistry())
    old = await pool._entry_stub("voice-same")
    await old.lock.acquire()
    invalidation = asyncio.create_task(pool.invalidate_all())
    replacement = None
    closing = None
    try:
        while "voice-same" in pool._entries:
            await asyncio.sleep(0)
        replacement = asyncio.create_task(pool._entry_stub("voice-same"))
        await asyncio.sleep(0)
        assert not replacement.done()

        # #853: a 1 s window, so the "still draining" observation below is
        # about the mechanism (no force-close before the window ends), not a
        # race against a 10 ms fuse.
        closing = asyncio.create_task(pool.aclose(drain_timeout=1.0))
        with pytest.raises(PoolUnavailable, match="pool closing"):
            await asyncio.wait_for(replacement, timeout=1)
        assert "voice-same" not in pool._entries
        assert not closing.done()

        old.lock.release()
        await asyncio.wait_for(invalidation, timeout=1)
        await asyncio.wait_for(closing, timeout=1)
        assert not pool._invalidation_groups
    finally:
        if old.lock.locked():
            old.lock.release()
        await asyncio.gather(
            *(task for task in (invalidation, closing) if task is not None),
            return_exceptions=True,
        )
        if replacement is not None and not replacement.done():
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)


async def test_turn_on_closing_pool_raises_poolunavailable():
    from sdk_client_pool import PoolUnavailable
    reg = FakeRegistry()
    pool = _mk_pool(reg)
    await pool.aclose()
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    with pytest.raises(PoolUnavailable):
        await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                        origin={}, cid="c", build_options=build_options,
                        on_stale_old=lambda s, g=None: None, on_message=on_message)
    # Finding 2: sweeper should not be created on turn() after aclose()
    assert pool._sweeper is None


async def test_per_agent_cap_lru_evicts(monkeypatch):
    monkeypatch.setenv("SDK_POOL_MAX_PER_AGENT", "2")
    reg = FakeRegistry()
    for i in range(3):
        reg.data[f"v-{i}"] = {"sdk_session_id": f"sid-{i}", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go(i):
        return await pool.turn(channel_key=f"v-{i}", channel="voice",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s, g=None: None,
                               on_message=on_message)
    for i in range(3):
        t = asyncio.create_task(go(i)); await asyncio.sleep(0.01)
        made[-1].script = [[_mk_result(f"sid-{i}")]]
        await t
    assert pool.stats()["entries"] == 2
    assert made[0].disconnected          # v-0 was least-recently-used


async def test_sweeper_closes_idle_and_overage(monkeypatch):
    now = [1000.0]
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "sid-1", "last_active": "x"}
    pool = _mk_pool(reg, monotonic=lambda: now[0])
    pool.idle_seconds = 100.0
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s, g=None: None, on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-1")]]
    await t
    now[0] += 50
    await pool._sweep_once()
    assert pool.stats()["entries"] == 1
    now[0] += 100                          # beyond idle bound
    await pool._sweep_once()
    assert pool.stats()["entries"] == 0
    assert made[0].disconnected


async def test_idle_bound_clamped_to_freshness(monkeypatch):
    """AR-4: idle can never exceed the channel freshness window."""
    from datetime import timedelta
    now = [0.0]
    reg = FakeRegistry()
    reg.data["voice-1"] = {"sdk_session_id": "s", "last_active": "x"}
    pool = _mk_pool(reg, monotonic=lambda: now[0])
    pool.idle_seconds = 10_000_000.0       # operator misconfig
    pool._freshness = lambda ch: timedelta(minutes=30)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="voice-1", channel="voice",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s, g=None: None,
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("s")]]
    await t
    now[0] += 31 * 60                      # 31 min > freshness 30 min
    await pool._sweep_once()
    assert pool.stats()["entries"] == 0


async def test_fleet_cap_across_pools(monkeypatch):
    monkeypatch.setenv("SDK_POOL_FLEET_CAP", "1")
    reg = FakeRegistry()
    reg.data["a-1"] = {"sdk_session_id": "s1", "last_active": "x"}
    reg.data["b-1"] = {"sdk_session_id": "s2", "last_active": "x"}
    p1, p2 = _mk_pool(reg), _mk_pool(reg)
    made = []
    for p in (p1, p2):
        p._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go(pool, key):
        return await pool.turn(channel_key=key, channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s, g=None: None, on_message=on_message)
    t = asyncio.create_task(go(p1, "a-1")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s1")]]
    await t
    t = asyncio.create_task(go(p2, "b-1")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s2")]]
    await t
    assert p1.stats()["entries"] + p2.stats()["entries"] == 1
    await p1.aclose(); await p2.aclose()


async def test_fleet_cap_lru_tie_across_pools(monkeypatch):
    """Identical last_used in two DISTINCT pools must not TypeError the fleet LRU.

    FLEET_CAP=2, frozen identical monotonic on both pools. Turn 3 (p2's second
    key) pushes the fleet to 3 and calls `_enforce_caps(protect="b-2")` on p2.
    Candidates at that min() call are p1's "a-1" AND p2's "b-1" — two warm
    entries, tied last_used, from two DISTINCT pool objects — so the
    pre-fix `min(candidates, default=None)` falls through the tie to
    comparing (p1, ...) < (p2, ...) tuples, i.e. comparing SdkClientPool
    objects directly -> TypeError. (With only one candidate, as the prior
    single-entry-per-pool version of this test had, min() never compares
    anything and the bug is unreachable.)
    """
    monkeypatch.setenv("SDK_POOL_FLEET_CAP", "2")
    frozen = 1000.0
    reg = FakeRegistry()
    reg.data["a-1"] = {"sdk_session_id": "s1", "last_active": "x"}
    reg.data["b-1"] = {"sdk_session_id": "s2", "last_active": "x"}
    reg.data["b-2"] = {"sdk_session_id": "s3", "last_active": "x"}
    p1 = _mk_pool(reg, monotonic=lambda: frozen)
    p2 = _mk_pool(reg, monotonic=lambda: frozen)
    made = []
    for p in (p1, p2):
        p._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go(pool, key):
        return await pool.turn(channel_key=key, channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s, g=None: None, on_message=on_message)
    # Turn 1: p1 gets "a-1" — fleet=1, no eviction.
    t = asyncio.create_task(go(p1, "a-1")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s1")]]
    await t
    # Turn 2: p2 gets "b-1" — fleet=2, no eviction.
    t = asyncio.create_task(go(p2, "b-1")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s2")]]
    await t
    # Turn 3: p2 gets "b-2" — fleet=3 > cap=2, forces _enforce_caps(protect="b-2")
    # on p2. Candidates: p1's warm "a-1" and p2's warm "b-1", tied last_used,
    # from two distinct pool objects.
    t = asyncio.create_task(go(p2, "b-2")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s3")]]
    await t
    assert p1.stats()["entries"] + p2.stats()["entries"] == 2
    # Exactly one of the tied "a-1"/"b-1" clients was the LRU victim; "b-2"
    # (the just-used, protected key) must survive.
    a1_client, b1_client, b2_client = made[0], made[1], made[2]
    assert a1_client.disconnected != b1_client.disconnected  # exactly one evicted
    assert not b2_client.disconnected
    await p1.aclose(); await p2.aclose()


async def test_evict_waits_for_entry_lock():
    """AR-7: sweep/LRU eviction must not disconnect a client whose entry
    lock is held (warm-window race: lock acquired, state still 'warm')."""
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "s", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s, g=None: None, on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("s")]]
    await t
    entry = pool._entries["v-1"]
    async with entry.lock:                      # simulate the warm window
        evict = asyncio.create_task(pool._evict("v-1", entry))
        await asyncio.sleep(0.05)
        assert not evict.done()                 # eviction is waiting, not closing
        assert not made[0].disconnected
    await evict
    assert made[0].disconnected
    assert pool.stats()["entries"] == 0


async def test_close_key_joins_inflight_invalidation_flush():
    """#319: close_key (the /new reset hook) must not return while an
    invalidate_all() generation for the same key is still draining — the reset's
    save may otherwise read a transcript the old client has not flushed, and the
    finishing old turn can resurrect the just-reset session."""
    reg = FakeRegistry()
    reg.data["voice-same"] = {"sdk_session_id": "sid-same", "last_active": "x"}
    pool = _mk_pool(reg)
    first_turn_started = asyncio.Event()
    release_first_turn = asyncio.Event()
    old_disconnect_started = asyncio.Event()
    release_old_disconnect = asyncio.Event()

    class GatedClient(ScriptedClient):
        async def query(self, prompt, session_id="default"):
            self.queries.append(prompt)
            if prompt == "first":
                first_turn_started.set()

        async def receive_response(self):
            if self.queries[-1] == "first":
                await release_first_turn.wait()
            yield _mk_result("sid-same")

        async def disconnect(self):
            self.disconnected = True
            old_disconnect_started.set()
            await release_old_disconnect.wait()

    pool._make_client = GatedClient

    async def go(prompt):
        async def build_options(is_fresh, resume_sid):
            return {"resume": resume_sid}

        async def on_message(_message):
            return None

        return await pool.turn(
            channel_key="voice-same", channel="voice", prompt=prompt,
            origin={}, cid="c", build_options=build_options,
            on_stale_old=lambda _sid, _g=None: None, on_message=on_message,
        )

    first = asyncio.create_task(go("first"))
    invalidation = None
    closer = None
    try:
        await asyncio.wait_for(first_turn_started.wait(), timeout=1)
        invalidation = asyncio.create_task(pool.invalidate_all())
        await asyncio.sleep(0)  # invalidation snapshots + blocks on entry lock

        closer = asyncio.create_task(pool.close_key("voice-same"))
        for _ in range(5):
            await asyncio.sleep(0)
        # Old turn still owns the entry lock — the reset must be waiting.
        assert not closer.done(), "close_key returned before the old turn ended"

        release_first_turn.set()
        assert (await asyncio.wait_for(first, timeout=1)).sid == "sid-same"
        await asyncio.wait_for(old_disconnect_started.wait(), timeout=1)
        for _ in range(5):
            await asyncio.sleep(0)
        # The old generation's transport close (= transcript flush, AR-4) is
        # still in flight — the reset must keep waiting for it.
        assert not closer.done(), "close_key returned before the old client flushed"

        release_old_disconnect.set()
        await asyncio.wait_for(closer, timeout=1)
        await asyncio.wait_for(invalidation, timeout=1)
    finally:
        release_first_turn.set()
        release_old_disconnect.set()
        tasks = [t for t in (first, invalidation, closer) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await pool.aclose()


# ---------------------------------------------------------------------------
# #290/#411 — retirement-claim steer at the pool decision site
# ---------------------------------------------------------------------------


class RetiringRegistry(FakeRegistry):
    """FakeRegistry + the retirement surface the real SessionRegistry grew."""

    def __init__(self):
        super().__init__()
        self.retiring: set[str] = set()

    def retirement_pending(self, key):
        return key in self.retiring


async def test_retirement_steers_turn_fresh_and_skips_stale_retain():
    """#290 red case: a fresh registry entry would resume, but a live
    retirement claim must force a FRESH session, never resume the dying sid,
    and must NOT run on_stale_old (the retiring caller owns the retain)."""
    reg = RetiringRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-dying", "last_active": "x"}
    reg.retiring.add("tg-1")
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    stale_calls = []
    decisions = []

    async def build_options(is_fresh, resume_sid):
        return {"fresh": is_fresh, "resume": resume_sid}

    async def on_message(m):
        pass

    async def go():
        return await pool.turn(
            channel_key="tg-1", channel="telegram", prompt="hi",
            origin={}, cid="c", build_options=build_options,
            on_stale_old=lambda s, g=None: stale_calls.append(s),
            on_message=on_message,
            on_decision=lambda sid, fresh, gen: decisions.append((sid, fresh)),
        )

    t = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-new")]]
    res = await t
    assert res.is_fresh is True and res.resume_sid is None
    assert made[0].options == {"fresh": True, "resume": None}
    assert stale_calls == []          # no duplicate retain of the dying session
    assert decisions == [(None, True)]
    await pool.aclose()


async def test_no_retirement_resumes_normally_on_same_registry():
    """Sanity twin: the same registry WITHOUT the claim resumes the sid —
    proving the steer (not the harness) forced fresh above."""
    reg = RetiringRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-live", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]

    async def build_options(is_fresh, resume_sid):
        return {"resume": resume_sid}

    async def on_message(m):
        pass

    async def go():
        return await pool.turn(
            channel_key="tg-1", channel="telegram", prompt="hi",
            origin={}, cid="c", build_options=build_options,
            on_stale_old=lambda s, g=None: None, on_message=on_message)

    t = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-live")]]
    res = await t
    assert res.resume_sid == "sid-live" and res.is_fresh is False
    await pool.aclose()


async def test_on_stale_old_receives_decisions_fence_generation():
    """#411 (design r4, Sol): the fence generation travels FROM THE DECISION
    into on_stale_old — the pool must pass through whatever the decide
    callback captured, not re-sample after the close await."""
    reg = FakeRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-old", "last_active": "x"}
    import dataclasses as _dc

    def decide_new_retain(channel, entry, now):
        return _dc.replace(
            _new_dec(entry, retain_old=True), fence_generation=41,
        )

    pool = _mk_pool(reg, decide=decide_new_retain)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    received = []

    async def build_options(is_fresh, resume_sid):
        return {}

    async def on_message(m):
        pass

    async def go():
        return await pool.turn(
            channel_key="tg-1", channel="telegram", prompt="hi",
            origin={}, cid="c", build_options=build_options,
            on_stale_old=lambda s, g=None: received.append(g),
            on_message=on_message)

    t = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-f")]]
    await t
    assert received == [41]
    await pool.aclose()


# --- #853: the drain timeout bounds every generation, invalidated ones included ---


class CountedDisconnectClient(ScriptedClient):
    """SDK-boundary fake that counts disconnect STARTS and COMPLETIONS
    separately: a completion is recorded only after ``disconnect()`` returned,
    so a cancelled disconnect can never masquerade as a completed one. When
    ``release_disconnect`` is an Event the disconnect parks on it."""

    def __init__(self, options):
        super().__init__(options)
        self.disconnect_starts = 0
        self.disconnect_completions = 0
        self.disconnect_started = asyncio.Event()
        self.release_disconnect = None

    async def disconnect(self):
        self.disconnect_starts += 1
        self.disconnect_started.set()
        if self.release_disconnect is not None:
            await self.release_disconnect.wait()
        await super().disconnect()
        self.disconnect_completions += 1


async def _drain_test_tasks(pool, old, *tasks):
    """Cleanup for the #853 pins: release the held lock, give every task a
    bounded chance to finish, cancel only what is still pending, consume every
    result, and drop the pool from the fleet list."""
    from sdk_client_pool import SdkClientPool
    if old.lock.locked():
        old.lock.release()
    live = [t for t in tasks if t is not None]
    if live:
        await asyncio.wait(live, timeout=1)
        for t in live:
            if not t.done():
                t.cancel()
        await asyncio.gather(*live, return_exceptions=True)
    if pool in SdkClientPool._instances:
        SdkClientPool._instances.remove(pool)


async def test_pool_close_force_disconnects_invalidated_generation_without_waiting_for_turn_lock():
    """Red case for #853 (declares INV-TURN-010): a generation handed to
    ``invalidate_all`` whose turn still holds the entry lock when the drain
    window ends is force-disconnected and ``aclose`` returns — the closer is
    neither cancelled nor waited on further, and finishes its bookkeeping
    when the turn releases."""
    pool = _mk_pool(FakeRegistry())
    key = "voice-same"
    old = await pool._entry_stub(key)
    client = CountedDisconnectClient(None)
    old._client = client
    old.state = "warm"
    await old.lock.acquire()                      # the wedged turn
    invalidation = asyncio.create_task(pool.invalidate_all())
    closing = None
    workers = set()
    try:
        async def handed_off():
            while key in pool._entries:
                await asyncio.sleep(0)

        await asyncio.wait_for(handed_off(), timeout=1)
        workers = set(pool._invalidation_closes[key])
        barrier = pool._invalidation_barriers[key]

        assert len(pool._entries) == 0
        assert len(workers) == 1
        assert len(pool._invalidation_groups) == 1

        # P0: invalidation alone must not disconnect the held generation.
        await asyncio.sleep(0.1)
        assert (client.disconnect_starts, client.disconnect_completions) == (0, 0)
        assert sum(t.done() for t in workers) == 0
        assert not barrier.done()

        closing = asyncio.create_task(pool.aclose(drain_timeout=0.05))

        # A1: observe completion without cancelling the operation under test.
        done, pending = await asyncio.wait({closing}, timeout=1)
        assert (len(done), len(pending)) == (1, 0)
        assert closing.result() is None

        # A2: shutdown neither cancels nor finishes the held-lock closer.
        assert sum(t.done() for t in workers) == 0
        assert sum(bool(t.cancelled() or t.cancelling()) for t in workers) == 0
        assert not invalidation.done()

        # A3: transport disconnect completed exactly once.
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)

        # A4: shutdown did not release the turn's lock.
        assert old.lock.locked()

        # A5: the original closer remains registered for close_key to join.
        assert pool._invalidation_closes.get(key) == workers
        assert len(pool._invalidation_groups) == 1

        old.lock.release()
        await asyncio.wait_for(asyncio.shield(invalidation), timeout=1)

        # A6: eventual closer completion neither repeats disconnect nor leaks
        # ownership.
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)
        assert (len(pool._invalidation_groups),
                len(pool._invalidation_closes)) == (0, 0)
        assert sum(t.cancelled() for t in workers) == 0
        assert all(t.result() is None for t in workers)
    finally:
        await _drain_test_tasks(pool, old, invalidation, closing, *workers)


async def test_pool_close_joins_invalidated_generation_disconnect_already_started():
    """#853 join regression (passes at base, where the unbounded gather joined
    everything): a closer already inside its transport disconnect when the
    drain window ends is JOINED — ``aclose`` returns only after that disconnect
    completes, and the disconnect runs exactly once."""
    pool = _mk_pool(FakeRegistry())
    key = "voice-same"
    old = await pool._entry_stub(key)
    client = CountedDisconnectClient(None)
    client.release_disconnect = asyncio.Event()
    old._client = client
    old.state = "warm"
    invalidation = asyncio.create_task(pool.invalidate_all())
    closing = None
    workers = set()
    try:
        await asyncio.wait_for(client.disconnect_started.wait(), timeout=1)
        workers = set(pool._invalidation_closes.get(key, ()))
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 0)

        closing = asyncio.create_task(pool.aclose(drain_timeout=0.05))
        done, pending = await asyncio.wait({closing}, timeout=0.2)
        # J1: the window has passed; shutdown still waits on the disconnect.
        assert (len(done), len(pending)) == (0, 1)
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 0)

        client.release_disconnect.set()
        await asyncio.wait_for(asyncio.shield(closing), timeout=1)
        await asyncio.wait_for(asyncio.shield(invalidation), timeout=1)
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)
        assert (len(pool._invalidation_groups),
                len(pool._invalidation_closes)) == (0, 0)
        assert sum(t.cancelled() for t in workers) == 0
    finally:
        client.release_disconnect.set()
        await _drain_test_tasks(pool, old, invalidation, closing, *workers)


async def _held_warm_entry(pool, key):
    """A warm entry at ``key`` with a counted fake attached and its lock HELD
    (the wedged turn). Returns (entry, client)."""
    entry = await pool._entry_stub(key)
    client = CountedDisconnectClient(None)
    entry._client = client
    entry.state = "warm"
    await entry.lock.acquire()
    return entry, client


async def test_force_close_hook_fires_before_disconnect_at_both_sites():
    """#853: BOTH force-close sites — the live-entry drain loop and the
    invalidated-generation arm — go through the one helper, which fires
    ``on_force_close(key, entry)`` before the transport is cut. A bypass of
    either site drops one pair."""
    seen = []

    def hook(key, entry):
        # Before the disconnect: nothing started yet on this entry's client.
        seen.append((key, entry, entry._client.disconnect_starts))

    pool = _mk_pool(FakeRegistry(), on_force_close=hook)
    invalidated, inv_client = await _held_warm_entry(pool, "voice-inv")
    invalidation = asyncio.create_task(pool.invalidate_all())
    closing = None
    try:
        while "voice-inv" in pool._entries:
            await asyncio.sleep(0)
        live, live_client = await _held_warm_entry(pool, "voice-live")
        closing = asyncio.create_task(pool.aclose(drain_timeout=0.05))
        done, _pending = await asyncio.wait({closing}, timeout=1)
        assert len(done) == 1 and closing.result() is None
        assert seen == [("voice-live", live, 0), ("voice-inv", invalidated, 0)]
        assert (live_client.disconnect_completions,
                inv_client.disconnect_completions) == (1, 1)
        assert live.lock.locked() and invalidated.lock.locked()
    finally:
        if "live" in locals() and live.lock.locked():
            live.lock.release()
        await _drain_test_tasks(pool, invalidated, invalidation, closing)


async def test_force_close_hook_exception_is_contained():
    """#853: a raising hook never unwinds ``aclose`` — both entries are still
    force-closed and the pool close completes."""
    calls = []

    def hook(key, entry):
        calls.append(key)
        raise RuntimeError("consumer not ready")

    pool = _mk_pool(FakeRegistry(), on_force_close=hook)
    invalidated, inv_client = await _held_warm_entry(pool, "voice-inv")
    invalidation = asyncio.create_task(pool.invalidate_all())
    closing = None
    try:
        while "voice-inv" in pool._entries:
            await asyncio.sleep(0)
        live, live_client = await _held_warm_entry(pool, "voice-live")
        closing = asyncio.create_task(pool.aclose(drain_timeout=0.05))
        done, _pending = await asyncio.wait({closing}, timeout=1)
        assert len(done) == 1 and closing.result() is None
        assert calls == ["voice-live", "voice-inv"]
        assert (live_client.disconnect_completions,
                inv_client.disconnect_completions) == (1, 1)
    finally:
        if "live" in locals() and live.lock.locked():
            live.lock.release()
        await _drain_test_tasks(pool, invalidated, invalidation, closing)


async def test_pool_close_default_hook_is_a_noop_and_injectable():
    from sdk_client_pool import _no_force_close_hook
    pool = _mk_pool(FakeRegistry())
    assert pool.on_force_close is _no_force_close_hook
    assert _no_force_close_hook("k", object()) is None
    marks = []
    pool.on_force_close = lambda key, entry: marks.append(key)
    live, client = await _held_warm_entry(pool, "voice-live")
    try:
        await asyncio.wait_for(pool.aclose(drain_timeout=0.05), timeout=1)
        assert marks == ["voice-live"] and client.disconnect_completions == 1
    finally:
        live.lock.release()


async def test_concurrent_pool_close_does_not_force_close_the_other_drains_live_entry_early():
    """#853 (seam round): a second ``aclose`` overlapping the first — reload
    teardown overlapping container shutdown; ``Agent.aclose`` is documented
    safe to call twice — must NOT treat the first call's under-drain live
    entry as a timed-out closer and force-close it before ITS window."""
    from sdk_client_pool import DrainOwner
    pool = _mk_pool(FakeRegistry())
    live, client = await _held_warm_entry(pool, "voice-live")
    close_a = asyncio.create_task(pool.aclose(drain_timeout=0.3))
    close_b = None
    try:
        while live not in pool._draining:
            await asyncio.sleep(0)
        assert pool._draining[live] == DrainOwner("voice-live", None)
        close_b = asyncio.create_task(pool.aclose(drain_timeout=0.3))
        done, _pending = await asyncio.wait({close_a, close_b}, timeout=0.15)
        # Before either window: neither close finished, nothing disconnected.
        assert len(done) == 0
        assert (client.disconnect_starts, client.disconnect_completions) == (0, 0)
        assert live.lock.locked()
        done, _pending = await asyncio.wait({close_a, close_b}, timeout=1)
        assert len(done) == 2
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)
        assert live not in pool._draining
    finally:
        live.lock.release()
        await _drain_test_tasks(pool, live, close_a, close_b)


async def test_pool_close_re_drains_an_entry_a_cancelled_close_left_mid_drain():
    """#853: an outer bound (the container's 15 s ``wait_for``) that cancels a
    close mid-drain leaves its live entries recorded; a later close re-drains
    them with its own full window, then force-closes."""
    pool = _mk_pool(FakeRegistry())
    live, client = await _held_warm_entry(pool, "voice-live")
    close_a = asyncio.create_task(pool.aclose(drain_timeout=5))
    close_b = None
    try:
        while live not in pool._draining:
            await asyncio.sleep(0)
        close_a.cancel()
        await asyncio.gather(close_a, return_exceptions=True)
        assert close_a.cancelled()
        assert live in pool._draining and client.disconnect_starts == 0
        close_b = asyncio.create_task(pool.aclose(drain_timeout=0.2))
        done, _pending = await asyncio.wait({close_b}, timeout=0.1)
        assert len(done) == 0 and client.disconnect_starts == 0
        done, _pending = await asyncio.wait({close_b}, timeout=1)
        assert len(done) == 1 and close_b.result() is None
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)
        assert live not in pool._draining and live.lock.locked()
    finally:
        live.lock.release()
        await _drain_test_tasks(pool, live, close_a, close_b)


async def test_pool_close_joins_a_turn_owned_disconnect_already_started():
    """#853 (seam round): the disconnect a FAILED TURN started through
    ``_invalidate`` (state ``invalid``, client detached, no lock release yet)
    is joined by the force-close — ``aclose`` returns only once it completes,
    and it runs exactly once."""
    pool = _mk_pool(FakeRegistry())
    old, client = await _held_warm_entry(pool, "voice-same")
    client.release_disconnect = asyncio.Event()
    turn_invalidate = asyncio.create_task(old._invalidate())   # under the held lock
    invalidation = closing = None
    try:
        await asyncio.wait_for(client.disconnect_started.wait(), timeout=1)
        assert old.state == "invalid" and old._client is None
        invalidation = asyncio.create_task(pool.invalidate_all())
        while "voice-same" in pool._entries:
            await asyncio.sleep(0)
        closing = asyncio.create_task(pool.aclose(drain_timeout=0.05))
        done, _pending = await asyncio.wait({closing}, timeout=0.2)
        assert (len(done), client.disconnect_starts, client.disconnect_completions) == (0, 1, 0)
        client.release_disconnect.set()
        await asyncio.wait_for(asyncio.shield(closing), timeout=1)
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)
        assert old.state == "closed" and old.lock.locked()
    finally:
        client.release_disconnect.set()
        await _drain_test_tasks(pool, old, turn_invalidate, invalidation, closing)


async def test_draining_records_name_the_closer_task_then_the_drain_loop():
    """#853: the retained ownership a later change enumerates — an
    invalidated entry's record names its closer task (the one registered for
    ``close_key`` to join); an entry the close loop popped names none."""
    from sdk_client_pool import DrainOwner
    pool = _mk_pool(FakeRegistry())
    old, _client = await _held_warm_entry(pool, "voice-inv")
    invalidation = asyncio.create_task(pool.invalidate_all())
    closing = None
    try:
        while "voice-inv" in pool._entries:
            await asyncio.sleep(0)
        (closer,) = pool._invalidation_closes["voice-inv"]
        assert pool._draining[old] == DrainOwner("voice-inv", closer)
        live, _c = await _held_warm_entry(pool, "voice-live")
        closing = asyncio.create_task(pool.aclose(drain_timeout=0.05))
        while live not in pool._draining:
            await asyncio.sleep(0)
        assert pool._draining[live] == DrainOwner("voice-live", None)
        await asyncio.wait_for(asyncio.shield(closing), timeout=1)
        assert live not in pool._draining          # closed by the loop
        assert pool._draining[old].closer is closer  # still owned by the closer
        old.lock.release()
        await asyncio.wait_for(asyncio.shield(invalidation), timeout=1)
        assert pool._draining == {}
    finally:
        if "live" in locals() and live.lock.locked():
            live.lock.release()
        await _drain_test_tasks(pool, old, invalidation, closing)


async def test_schedule_agent_close_task_ends_at_the_drain_timeout_with_the_lock_still_held():
    """#853 ARM3 through the real ``reload._schedule_agent_close``: the
    ``agent-pool-close`` background task leaves ``_AGENT_CLOSE_TASKS`` within
    the drain window while the invalidated generation's turn still holds its
    lock — the reload report no longer shows the role draining forever."""
    import reload as R
    pool = _mk_pool(FakeRegistry())
    old, client = await _held_warm_entry(pool, "voice-same")
    invalidation = asyncio.create_task(pool.invalidate_all())

    class AgentLike:
        async def aclose(self):
            await pool.aclose(drain_timeout=0.05)

    try:
        while "voice-same" in pool._entries:
            await asyncio.sleep(0)
        before = set(R._AGENT_CLOSE_TASKS)
        R._schedule_agent_close(AgentLike())
        (task,) = set(R._AGENT_CLOSE_TASKS) - before
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert len(done) == 1 and task.result() is None
        assert task not in R._AGENT_CLOSE_TASKS
        assert (client.disconnect_starts, client.disconnect_completions) == (1, 1)
        assert old.lock.locked() and not invalidation.done()
    finally:
        await _drain_test_tasks(pool, old, invalidation)


# --- #866: a replacement client is recorded BEFORE it connects -------------
# Declares INV-TURN-011. Every arm asserts COUNTS, never statuses, and every
# arm is red at the pre-fix tree: there, `turn` publishes the freshly opened
# client into `_entries` only AFTER `open()` returns, so a close path that
# enumerated the map first closes the transportless stub, returns, and leaves
# the real client connected and unreachable.


class GatedConnectClient(CountedDisconnectClient):
    """SDK-boundary fake whose ``connect()`` parks on ``release_connect``.

    Counts COMPLETED connects separately from starts, and tracks ``live`` — 1
    while a transport exists — as its own field rather than as
    ``connects - disconnects``, so a disconnect that lands BEFORE the connect
    completes cannot hide a resurrection (the whole point of arm 8).
    """

    def __init__(self, options):
        super().__init__(options)
        self.connect_completions = 0
        self.live = 0
        self.cancels_seen = 0
        self.connect_started = asyncio.Event()
        self.release_connect = asyncio.Event()
        self.suppress_cancel = False
        self.connect_raises: BaseException | None = None

    async def connect(self):
        self.connect_started.set()
        try:
            await self.release_connect.wait()
        except asyncio.CancelledError:
            self.cancels_seen += 1
            if not self.suppress_cancel:
                raise
            await self.release_connect.wait()
        if self.connect_raises is not None:
            raise self.connect_raises
        self.connected = True
        self.connect_completions += 1
        self.live = 1

    async def disconnect(self):
        await super().disconnect()
        self.live = 0


def _mk_gated_pool(registry=None, **kw):
    """A pool whose SDK clients are ``GatedConnectClient``s, plus the list of
    the ones actually constructed (``_make_client`` runs inside ``open()``, so
    an empty list means no connect was ever attempted)."""
    pool = _mk_pool(registry or FakeRegistry(), **kw)
    made: list[GatedConnectClient] = []

    def factory(options):
        made.append(GatedConnectClient(options))
        return made[-1]

    pool._make_client = factory
    return pool, made


def _start_turn(pool, key, *, binding_digest="", release_options=None):
    """Start one pooled turn as a task. ``release_options``, when given, is an
    Event the options build parks on — the window before any reservation."""

    async def build_options(is_fresh, resume_sid):
        if release_options is not None:
            await release_options.wait()
        return {"resume": resume_sid}

    async def on_message(_message):
        return None

    async def go():
        return await pool.turn(
            channel_key=key, channel="voice", prompt="p", origin={}, cid="c",
            build_options=build_options,
            on_stale_old=lambda snapshot, generation=None: None,
            on_message=on_message, binding_digest=binding_digest,
        )

    return asyncio.create_task(go())


def _counts(client, pool, returns):
    """(completed connects, completed disconnects, live transports, queries,
    mapped entries, normal returns from the close operation)."""
    return (
        client.connect_completions, client.disconnect_completions,
        client.live, len(client.queries), len(pool._entries), returns[0],
    )


async def _await_started(client):
    await asyncio.wait_for(client.connect_started.wait(), timeout=1)


async def _await_made(made, n=1):
    async def wait():
        while len(made) < n:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout=1)


async def _await_gone(pool, key):
    """Wait until a close path has removed ``key`` from the entry map."""
    async def wait():
        while key in pool._entries:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout=1)


def _hold_retry(pool):
    """Gate the turn's SECOND ``_entry_stub`` call, so an arm measures the
    retired generation rather than a legitimate replacement. Returns the Event
    that releases it."""
    gate = asyncio.Event()
    original = pool._entry_stub
    calls = [0]

    async def gated(channel_key):
        calls[0] += 1
        if calls[0] > 1:
            await gate.wait()
        return await original(channel_key)

    pool._entry_stub = gated
    return gate


async def _cleanup_866(pool, made, *tasks, gates=()):
    """Release every fake gate, give the tasks a bounded chance to finish,
    cancel what is still pending, consume every result, and drop the pool from
    the fleet list. Never releases an entry lock a live turn still holds."""
    from sdk_client_pool import SdkClientPool
    for gate in gates:
        gate.set()
    for client in made:
        client.release_connect.set()
        if client.release_disconnect is not None:
            client.release_disconnect.set()
    live = [t for t in tasks if t is not None]
    if live:
        await asyncio.wait(live, timeout=1)
        for t in live:
            if not t.done():
                t.cancel()
        await asyncio.gather(*live, return_exceptions=True)
    if pool in SdkClientPool._instances:
        SdkClientPool._instances.remove(pool)


async def test_866_arm1_cold_connect_overlapping_pool_close_is_disconnected():
    """Arm 1 — ``aclose`` overlapping a COLD connect, released only after the
    close has returned. Kills: deleting the pre-open reservation; deleting the
    connect cancellation."""
    pool, made = _mk_gated_pool()
    key, returns = "voice-cold", [0]
    turn = _start_turn(pool, key)
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)

        async def close():
            await pool.aclose(drain_timeout=0.05)
            returns[0] += 1

        closing = asyncio.create_task(close())
        done, pending = await asyncio.wait({closing}, timeout=2)
        assert (len(done), len(pending)) == (1, 0), "pool close did not return"
        assert closing.exception() is None
        assert _counts(client, pool, returns) == (0, 1, 0, 0, 0, 1)

        client.release_connect.set()
        await asyncio.wait({turn}, timeout=2)
        assert _counts(client, pool, returns) == (0, 1, 0, 0, 0, 1)
        assert client.cancels_seen == 1
    finally:
        await _cleanup_866(pool, made, turn, closing)


async def test_866_arm2_warm_replacement_overlapping_pool_close_is_disconnected():
    """Arm 2 — the same window entered by a WARM entry made non-reusable by a
    ``binding_digest`` change. A fix scoped to a cold connect misses this."""
    reg = FakeRegistry()
    reg.data["voice-warm"] = {"sdk_session_id": "sid", "last_active": "x",
                              "binding_digest": "old"}
    pool, made = _mk_gated_pool(reg)
    key, returns = "voice-warm", [0]

    old_entry = await pool._entry_stub(key)
    old_client = GatedConnectClient(None)
    old_client.connected = True
    old_client.connect_completions = 1
    old_client.live = 1
    old_entry._client = old_client
    old_entry.state = "warm"
    old_entry.sid = "sid"
    old_entry.binding_digest = "old"

    turn = _start_turn(pool, key, binding_digest="new")
    closing = None
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)
        # The old generation was flushed before the retain, as it always was.
        assert (old_client.connect_completions, old_client.disconnect_completions,
                old_client.live, len(old_client.queries)) == (1, 1, 0, 0)

        async def close():
            await pool.aclose(drain_timeout=0.05)
            returns[0] += 1

        closing = asyncio.create_task(close())
        done, pending = await asyncio.wait({closing}, timeout=2)
        assert (len(done), len(pending)) == (1, 0), "pool close did not return"
        assert _counts(client, pool, returns) == (0, 1, 0, 0, 0, 1)

        client.release_connect.set()
        await asyncio.wait({turn}, timeout=2)
        assert _counts(client, pool, returns) == (0, 1, 0, 0, 0, 1)
    finally:
        await _cleanup_866(pool, made + [old_client], turn, closing)


async def test_866_arm3_invalidate_all_overlapping_connect_disconnects_it():
    """Arm 3 — ``invalidate_all`` overlapping a connect. It never sets the
    closing flag, so a closing-flag re-check does not reach this arm. The
    retry is held at the second ``_entry_stub`` so the counts describe the
    RETIRED generation, not a legitimate replacement."""
    pool, made = _mk_gated_pool()
    key, returns = "voice-inv", [0]
    retry = _hold_retry(pool)
    turn = _start_turn(pool, key)
    invalidating = None
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)

        async def invalidate():
            await pool.invalidate_all()
            returns[0] += 1

        invalidating = asyncio.create_task(invalidate())
        await _await_gone(pool, key)

        client.release_connect.set()
        done, pending = await asyncio.wait({invalidating}, timeout=2)
        assert (len(done), len(pending)) == (1, 0), "invalidate_all did not return"
        assert invalidating.exception() is None
        assert _counts(client, pool, returns) == (1, 1, 0, 0, 0, 1)
    finally:
        await _cleanup_866(pool, made, turn, invalidating, gates=(retry,))


async def test_866_arm4_close_key_overlapping_connect_disconnects_it():
    """Arm 4 — ``close_key``, the /new reset listener, overlapping a connect.
    It never sets the closing flag either, and AR-4's guarantee is that the
    disconnect is what flushes the transcript — so the object it disconnects
    must be the one that holds it."""
    pool, made = _mk_gated_pool()
    key, returns = "voice-reset", [0]
    retry = _hold_retry(pool)
    turn = _start_turn(pool, key)
    resetting = None
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)

        async def reset():
            await pool.close_key(key)
            returns[0] += 1

        resetting = asyncio.create_task(reset())
        await _await_gone(pool, key)

        client.release_connect.set()
        done, pending = await asyncio.wait({resetting}, timeout=2)
        assert (len(done), len(pending)) == (1, 0), "close_key did not return"
        assert resetting.exception() is None
        assert _counts(client, pool, returns) == (1, 1, 0, 0, 0, 1)
    finally:
        await _cleanup_866(pool, made, turn, resetting, gates=(retry,))


async def test_866_arm5_close_cancelled_mid_drain_still_leaves_no_live_client():
    """Arm 5 — the pool close is CANCELLED during its lock drain (what
    ``_shutdown_cleanup``'s outer bound does). The turn, having lost ownership,
    must still disconnect the client it opened. Kills: deleting ``_drop`` from
    the failed post-open ownership branch."""
    pool, made = _mk_gated_pool()
    key, returns = "voice-cancel", [0]
    turn = _start_turn(pool, key)
    closing = None
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)

        async def close():
            await pool.aclose(drain_timeout=5)
            returns[0] += 1

        closing = asyncio.create_task(close())

        async def draining():
            while not pool._draining:
                await asyncio.sleep(0)
        await asyncio.wait_for(draining(), timeout=1)

        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)

        client.release_connect.set()
        await asyncio.wait({turn}, timeout=2)
        assert _counts(client, pool, returns) == (1, 1, 0, 0, 0, 0)
    finally:
        await _cleanup_866(pool, made, turn, closing)


async def test_866_arm6_force_close_hook_receives_the_connecting_generation():
    """Arm 6 — the force-close hook (#853's seam, and #847's prospective
    consumer) must be handed the client that actually holds a transport, not
    the transportless stub. Pins hook identity, not log wording."""
    hook_calls: list[tuple] = []
    pool, made = _mk_gated_pool(
        on_force_close=lambda key, entry: hook_calls.append(
            (key, entry, entry._client)),
    )
    key, returns = "voice-hook", [0]
    turn = _start_turn(pool, key)
    closing = None
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)

        async def close():
            await pool.aclose(drain_timeout=0.05)
            returns[0] += 1

        closing = asyncio.create_task(close())
        done, pending = await asyncio.wait({closing}, timeout=2)
        assert (len(done), len(pending)) == (1, 0), "pool close did not return"

        assert len(hook_calls) == 1
        assert sum(k == key and raw is client for k, _entry, raw in hook_calls) == 1
        assert _counts(client, pool, returns) == (0, 1, 0, 0, 0, 1)

        client.release_connect.set()
        await asyncio.wait({turn}, timeout=2)
        assert _counts(client, pool, returns) == (0, 1, 0, 0, 0, 1)
    finally:
        await _cleanup_866(pool, made, turn, closing)


async def test_866_arm7_closing_set_while_membership_remains_refuses_the_turn():
    """Arm 7 — the closing flag is set with the reservation STILL mapped (the
    window between ``aclose``'s unlocked write and its snapshot). This is the
    only arm that can kill a mutant deleting the closing conjunct of the
    post-open ownership check: ``aclose`` clears the map before its first drain
    wait, so every other arm is decided by map identity alone."""
    from sdk_client_pool import PoolUnavailable

    pool, made = _mk_gated_pool()
    key, returns = "voice-flag", [0]
    turn = _start_turn(pool, key)
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)

        pool._closing = True                 # no close is running
        client.release_connect.set()
        await asyncio.wait({turn}, timeout=2)
        assert _counts(client, pool, returns) == (1, 1, 0, 0, 0, 0)
        assert isinstance(turn.exception(), PoolUnavailable)
    finally:
        await _cleanup_866(pool, made, turn)


async def test_866_arm8_a_connect_that_suppresses_cancellation_is_still_cut():
    """Arm 8 — the connect catches the cancellation and completes anyway. The
    only arm that kills a mutant deleting the join on the connect task: without
    it the close disconnects and returns BEFORE the transport exists, and the
    connect then establishes a live one nobody owns."""
    pool, made = _mk_gated_pool()
    key, returns = "voice-suppress", [0]
    hook = asyncio.Event()
    pool.on_force_close = lambda k, e: hook.set()
    turn = _start_turn(pool, key)
    closing = None
    try:
        await _await_made(made)
        client = made[0]
        client.suppress_cancel = True
        await _await_started(client)

        async def close():
            await pool.aclose(drain_timeout=0.05)
            returns[0] += 1

        closing = asyncio.create_task(close())
        await asyncio.wait_for(hook.wait(), timeout=2)
        # Give the close a bounded window to (wrongly) finish while the
        # connect is still gated. Waiting on the cancellation being *seen*
        # would make the base red for an incidental timeout instead.
        await asyncio.wait({closing}, timeout=0.2)
        assert (returns[0], client.connect_completions,
                client.disconnect_completions, client.live,
                client.cancels_seen) == (0, 0, 0, 0, 1)
        assert len(client.queries) == 0
        assert len(pool._entries) == 0

        client.release_connect.set()
        done, pending = await asyncio.wait({closing}, timeout=2)
        assert (len(done), len(pending)) == (1, 0), "pool close did not return"
        await asyncio.wait({turn}, timeout=2)
        assert _counts(client, pool, returns) == (1, 1, 0, 0, 0, 1)
    finally:
        await _cleanup_866(pool, made, turn, closing)


async def test_866_arm9_closing_before_the_reservation_opens_no_client():
    """Arm 9 — the closing flag is set while the turn is still building its
    options, before any reservation. Nothing may be connected at all. Kills a
    mutant deleting the closing conjunct of the PRE-open reservation."""
    from sdk_client_pool import PoolUnavailable

    pool, made = _mk_gated_pool()
    key = "voice-preclose"
    options = asyncio.Event()
    turn = _start_turn(pool, key, release_options=options)
    try:
        async def building():
            while key not in pool._entries:
                await asyncio.sleep(0)
        await asyncio.wait_for(building(), timeout=1)

        pool._closing = True                 # the stub is still mapped
        options.set()
        await asyncio.wait({turn}, timeout=2)
        # No SDK client was ever constructed, so none can leak.
        assert (len(made), len(pool._entries)) == (0, 1)
        assert isinstance(turn.exception(), PoolUnavailable)
    finally:
        await _cleanup_866(pool, made, turn, gates=(options,))


async def test_866_arm10_a_reset_before_the_reservation_opens_no_client():
    """Arm 10 — ``close_key`` pops the stub while the turn is still building
    its options. Kills a mutant deleting the identity conjunct of the PRE-open
    reservation."""
    pool, made = _mk_gated_pool()
    key = "voice-prereset"
    options = asyncio.Event()
    retry = _hold_retry(pool)
    turn = _start_turn(pool, key, release_options=options)
    resetting = None
    try:
        async def building():
            while key not in pool._entries:
                await asyncio.sleep(0)
        await asyncio.wait_for(building(), timeout=1)

        resetting = asyncio.create_task(pool.close_key(key))
        await _await_gone(pool, key)

        options.set()
        done, pending = await asyncio.wait({resetting}, timeout=2)
        # No SDK client was ever constructed, so none can leak.
        assert (len(made), len(pool._entries)) == (0, 0)
        assert (len(done), len(pending)) == (1, 0), "close_key did not return"
        assert resetting.exception() is None
    finally:
        await _cleanup_866(pool, made, turn, resetting, gates=(options, retry))


async def test_866_arm11_a_failed_connect_strands_no_map_entry():
    """Arm 11 — the connect raises. The reservation must not be left behind as
    a dead map entry. Kills a mutant deleting ``_drop`` from the open-failure
    branch. Red at the pre-fix tree too, where the STUB is stranded instead."""
    pool, made = _mk_gated_pool()
    key = "voice-openfail"
    turn = _start_turn(pool, key)
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)
        client.connect_raises = RuntimeError("connect failed")
        client.release_connect.set()

        await asyncio.wait({turn}, timeout=2)
        assert isinstance(turn.exception(), RuntimeError)
        assert (client.connect_completions, client.disconnect_completions,
                client.live, len(pool._entries)) == (0, 1, 0, 0)
    finally:
        await _cleanup_866(pool, made, turn)


async def test_force_close_warning_says_whether_there_was_a_transport(caplog):
    """#866: the force-close warning named a key and claimed a cut it had
    often not made — the object it force-closed was the transportless stub.
    It now reports which, so an operator reading the line can tell an
    interrupted turn from a bookkeeping close. Not part of the frozen red
    case; the arms there pin hook identity, not log wording."""
    import logging

    # (a) a turn parked BEFORE its reservation: the stub is what gets forced.
    pool, made = _mk_gated_pool()
    options = asyncio.Event()
    turn = _start_turn(pool, "voice-stub", release_options=options)
    try:
        async def mapped():
            while "voice-stub" not in pool._entries:
                await asyncio.sleep(0)
        await asyncio.wait_for(mapped(), timeout=1)
        with caplog.at_level(logging.WARNING, logger="sdk_client_pool"):
            await pool.aclose(drain_timeout=0.05)
        lines = [r.getMessage() for r in caplog.records
                 if "force close" in r.getMessage()]
        assert lines == ["pool aclose: drain timeout on voice-stub; "
                         "force close (transport=False)"]
        assert len(made) == 0
    finally:
        await _cleanup_866(pool, made, turn, gates=(options,))

    # (b) a turn that has connected and is parked in its query, still holding
    # the entry lock: the reserved client is what gets forced.
    caplog.clear()

    class _QueryParkingClient(GatedConnectClient):
        def __init__(self, options):
            super().__init__(options)
            self.release_query = asyncio.Event()

        async def query(self, prompt, session_id="default"):
            await super().query(prompt, session_id)
            await self.release_query.wait()

    pool, made = _mk_gated_pool()
    pool._make_client = lambda options: (
        made.append(_QueryParkingClient(options)) or made[-1])
    turn = _start_turn(pool, "voice-live")
    try:
        await _await_made(made)
        client = made[0]
        await _await_started(client)
        client.release_connect.set()

        async def querying():
            while not client.queries:
                await asyncio.sleep(0)
        await asyncio.wait_for(querying(), timeout=1)

        with caplog.at_level(logging.WARNING, logger="sdk_client_pool"):
            await pool.aclose(drain_timeout=0.05)
        lines = [r.getMessage() for r in caplog.records
                 if "force close" in r.getMessage()]
        assert lines == ["pool aclose: drain timeout on voice-live; "
                         "force close (transport=True)"]
        assert (client.disconnect_completions, client.live) == (1, 0)
    finally:
        client.release_query.set()
        await _cleanup_866(pool, made, turn)
