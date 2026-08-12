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
            on_stale_old=lambda s: None, on_message=on_message)

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
            on_stale_old=lambda _sid: None,
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
                on_stale_old=lambda _sid: None,
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
                    on_stale_old=lambda _sid: None,
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
                on_stale_old=lambda _sid: None,
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
                               on_stale_old=lambda s: None,
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
                               on_stale_old=lambda s: None,
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
                               on_stale_old=lambda s: order.append(f"stale:{s.sdk_session_id}"),
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
                               on_stale_old=lambda s: None,
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
                               on_stale_old=lambda s: None, on_message=on_message)
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
                               on_stale_old=lambda s: None, on_message=on_message)
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
            on_stale_old=lambda _sid: None,
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

        closing = asyncio.create_task(pool.aclose(drain_timeout=0.01))
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
                        on_stale_old=lambda s: None, on_message=on_message)
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
                               on_stale_old=lambda s: None,
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
                               on_stale_old=lambda s: None, on_message=on_message)
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
                               on_stale_old=lambda s: None,
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
                               on_stale_old=lambda s: None, on_message=on_message)
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
                               on_stale_old=lambda s: None, on_message=on_message)
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
                               on_stale_old=lambda s: None, on_message=on_message)
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
            on_stale_old=lambda _sid: None, on_message=on_message,
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
