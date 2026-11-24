"""The running phone service: admission, the vault reads, the call slots, the
missing-key line, the stop, and the route that exists only with the line on.

The service side is the local fake; stores are real files in a temporary
directory. Numbers are invented."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from live_voice.session import Timing
from phone import service
from phone import settings
from phone import store as pstore
from phone.store import PhoneStore
from phone.webhook import PHONE_WEBHOOK_PATH, Incoming
from phone_fakes import FakeOpenAI, tool_call_event

DEFAULTS = (Path(__file__).resolve().parents[1]
            / "casa" / "rootfs" / "opt" / "casa" / "defaults")
KEY = "sk-test-not-a-real-key"
LINE = "+31201234567"

# Calls that stay up until stopped: nobody speaks and the clocks are long.
LONG = Timing(first_speech_s=5.0, quiet_s=5.0, silence_s=5.0, max_call_s=8.0,
              reconnect_s=1.0, decision_timeout_s=0.5,
              undecided_silence_s=1.0, goodbye_grace_s=0.3,
              speak_margin_s=0.0, pending_s=2.0)
# A call the clock ends quickly: the agent never speaks.
QUICK = Timing(first_speech_s=0.2, quiet_s=5.0, silence_s=5.0, max_call_s=8.0,
               reconnect_s=1.0, decision_timeout_s=0.5,
               undecided_silence_s=1.0, goodbye_grace_s=0.3,
               speak_margin_s=0.0, pending_s=2.0)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    root = tmp_path / "config"
    root.mkdir()
    monkeypatch.setenv("CASA_CONFIG_DIR", str(root))
    monkeypatch.setenv("CASA_DEFAULTS_DIR", str(DEFAULTS))
    monkeypatch.setenv("PHONE_ENABLED", "true")
    monkeypatch.delenv("PHONE_TRANSFER_NUMBER", raising=False)
    monkeypatch.delenv("LIVE_VOICES", raising=False)
    settings.reset()
    yield
    settings.reset()


@pytest.fixture
async def fake():
    async with FakeOpenAI() as server:
        yield server


@pytest.fixture
async def http():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def phone_store(tmp_path) -> PhoneStore:
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.open(), store.unavailable_reason
    return store


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Operator:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text) -> bool:
        self.lines.append(str(text))
        return True


class Vault:
    """``read_field`` stand-in: values per field, an optional gate a read
    blocks on (a real thread blocks, as ``op`` would), and a count."""

    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values
        self.reads: list[str] = []
        self.gate: threading.Event | None = None
        self.gate_field: str | None = None

    def __call__(self, title: str, field: str) -> str | None:
        assert title == "OpenAI Voice"
        self.reads.append(field)
        if self.gate is not None and field == self.gate_field:
            self.gate.wait(5)
        return self.values.get(field)


@pytest.fixture
async def running(fake, http, phone_store, monkeypatch):
    """Configure the service; yields a setter for its knobs; stops it."""
    monkeypatch.setattr(service, "STOP_WAIT_S", 0.5)
    op = Operator()
    vault = Vault({"credential": KEY, "webhook_secret": "whsec_x"})
    clock = Clock()

    def configure(*, timing: Timing = LONG, store=phone_store) -> None:
        service.configure(store=store, contacts=lambda: None, notify=op,
                          read_secret=vault, http=http, timing=timing,
                          clock=clock, base_url=fake.base_url)

    configure()
    try:
        yield configure, op, vault, clock
    finally:
        if vault.gate is not None:
            vault.gate.set()
        await service.stop()


def verbs(fake, verb: str) -> list[tuple[str, dict]]:
    return [(s, b) for v, s, b in fake.requests if v == verb]


async def wait_for(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def idle() -> None:
    await wait_for(lambda: not service._tasks)


def test_disabled_without_the_option(monkeypatch) -> None:
    monkeypatch.delenv("PHONE_ENABLED")
    settings.reset()
    assert service.enabled() is False


def test_enabled_with_the_option() -> None:
    assert service.enabled() is True


def _paths(app: web.Application) -> set[str]:
    return {r.canonical for r in app.router.resources()}


def test_route_absent_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("PHONE_ENABLED")
    settings.reset()
    app = web.Application()
    assert service.add_routes(app, default_region=lambda: "NL") is False
    assert PHONE_WEBHOOK_PATH not in _paths(app)


def test_route_present_when_enabled() -> None:
    app = web.Application()
    assert service.add_routes(app, default_region=lambda: "NL") is True
    assert PHONE_WEBHOOK_PATH in _paths(app)


async def test_key_is_cached_shared_and_none_never_cached(running) -> None:
    configure, op, vault, clock = running
    vault.gate = threading.Event()
    vault.gate_field = "credential"
    first = asyncio.ensure_future(service.key())
    second = asyncio.ensure_future(service.key())
    await asyncio.sleep(0.05)
    vault.gate.set()
    assert await first == KEY and await second == KEY
    assert vault.reads.count("credential") == 1
    assert await service.key() == KEY
    assert vault.reads.count("credential") == 1
    clock.t += 601
    assert await service.key() == KEY
    assert vault.reads.count("credential") == 2
    # A missing value is read again every time.
    vault.values["webhook_secret"] = None
    assert await service.secret() is None
    assert await service.secret() is None
    assert vault.reads.count("webhook_secret") == 2


async def test_blocked_vault_read_blocks_nothing_else(running, fake) -> None:
    configure, op, vault, clock = running
    configure(timing=QUICK)
    assert await service.key() == KEY          # cached for the first call
    service.on_incoming(Incoming("live_one", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept"))
    # Now a read that hangs in its thread.
    vault.gate = threading.Event()
    vault.gate_field = "webhook_secret"
    blocked = asyncio.ensure_future(service.secret())

    async def hello(request: web.Request) -> web.Response:
        return web.Response(text="hello")
    other = web.Application()
    other.router.add_get("/", hello)
    async with TestClient(TestServer(other)) as client:
        resp = await client.get("/")
        assert resp.status == 200 and await resp.text() == "hello"
    # The running call's clock still ends it.
    await wait_for(lambda: verbs(fake, "hangup"), timeout=3)
    assert not blocked.done()
    vault.gate.set()
    assert await blocked == "whsec_x"


async def test_third_call_is_rejected_busy(running, fake) -> None:
    for sid in ("live_a", "live_b", "live_c"):
        service.on_incoming(Incoming(sid, "+31612345678", LINE))
    await wait_for(lambda: len(verbs(fake, "accept")) == 2
                   and verbs(fake, "reject"))
    assert sorted(s for s, _ in verbs(fake, "accept")) == ["live_a", "live_b"]
    assert verbs(fake, "reject") == [("live_c", {"status_code": 486})]
    # The stop hangs up both running calls.
    await wait_for(lambda: fake.attach_count >= 2)
    await service.stop()
    assert sorted({s for s, _ in verbs(fake, "hangup")}) == [
        "live_a", "live_b"]
    assert not service._tasks


async def test_a_slot_is_free_again_after_a_call(running, fake) -> None:
    configure, op, vault, clock = running
    configure(timing=QUICK)
    for sid in ("live_a", "live_b"):
        service.on_incoming(Incoming(sid, "+31612345678", LINE))
    await idle()
    service.on_incoming(Incoming("live_c", "+31612345678", LINE))
    await idle()
    assert sorted(s for s, _ in verbs(fake, "accept")) == [
        "live_a", "live_b", "live_c"]
    assert verbs(fake, "reject") == []


async def test_missing_key_sends_nothing_and_tells_once_an_hour(
        running, fake) -> None:
    configure, op, vault, clock = running
    vault.values["credential"] = None
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await idle()
    assert fake.requests == [] and fake.attach_count == 0
    assert len(op.lines) == 1 and "OpenAI Voice" in op.lines[0]
    clock.t += 1800
    service.on_incoming(Incoming("live_b", "+31612345678", LINE))
    await idle()
    assert len(op.lines) == 1
    clock.t += 1801
    service.on_incoming(Incoming("live_c", "+31612345678", LINE))
    await idle()
    assert len(op.lines) == 2
    assert fake.requests == []


async def test_finish_sends_the_fallback_line(running, phone_store) -> None:
    configure, op, vault, clock = running
    with phone_store.transaction(write=True) as conn:
        pstore.insert_call(conn, {
            "id": "c1", "session_id": "live_x", "started_at": 100.0,
            "number": "+31612345678", "name": "", "caller_class": "unknown",
            "language": "nl", "returned": 0})
        pstore.finish_call(conn, "c1", ended_at=130.0,
                           outcome="caller_hung_up", actions=[], record=None)
    await service.finish("c1")
    assert len(op.lines) == 1
    assert "5678" in op.lines[0] and "+316123" not in op.lines[0]
    assert "30 s" in op.lines[0]
    with phone_store.transaction() as conn:
        assert pstore.get_call(conn, "c1")["delivered"] == 0
    # An unknown id is not an error.
    await service.finish("nope")


async def test_purge_never_raises(running, phone_store, tmp_path) -> None:
    configure, op, vault, clock = running
    with phone_store.transaction(write=True) as conn:
        pstore.insert_call(conn, {"id": "c1", "session_id": "live_x"})
        pstore.put_transcript(conn, "c1", "Caller: hi", 1.0)
    await service.purge()
    with phone_store.transaction() as conn:
        assert pstore.transcript(conn, "c1") is None
    configure(store=PhoneStore(str(tmp_path / "never-opened")))
    await service.purge()


async def test_no_call_is_admitted_after_the_stop(running, fake) -> None:
    await service.stop()
    service.on_incoming(Incoming("live_late", "+31612345678", LINE))
    assert not service._tasks
    assert fake.requests == []


async def test_a_failed_missing_key_line_does_not_silence_the_next(
        running, fake) -> None:
    configure, op, vault, clock = running
    vault.values["credential"] = None
    answers = [False, True]

    async def flaky(text) -> bool:
        op.lines.append(str(text))
        return answers.pop(0)
    service._notify = flaky
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await idle()
    service.on_incoming(Incoming("live_b", "+31612345678", LINE))
    await idle()
    assert len(op.lines) == 2
    service.on_incoming(Incoming("live_c", "+31612345678", LINE))
    await idle()
    assert len(op.lines) == 2


async def test_a_call_cut_by_the_stop_keeps_its_outcome(
        running, fake, phone_store, monkeypatch) -> None:
    monkeypatch.setattr(service, "STOP_WAIT_S", 2.0)
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept"))
    await fake.emit("live_a", {"type": "session.output_transcript.delta",
                               "delta": "Hello", "start_ms": 0,
                               "end_ms": 300})
    await fake.emit("live_a", {"type": "session.input_transcript.delta",
                               "delta": "Hi there", "start_ms": 400,
                               "end_ms": 800})

    async def provider() -> None:
        # The service closes a session it was asked to hang up.
        await wait_for(lambda: verbs(fake, "hangup"))
        await fake.close("live_a", "close_requested")
    closer = asyncio.create_task(provider())
    loop = asyncio.get_running_loop()
    began = loop.time()
    await service.stop()
    took = loop.time() - began
    await closer
    assert took < 2.0
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
        text = pstore.transcript(conn, row["id"])
    assert row["outcome"] == "interrupted"
    assert row["ended_at"] is not None
    assert text == "Assistant: Hello\nCaller: Hi there"
    assert not service._tasks


async def test_the_stop_is_bounded_when_a_session_never_closes(
        running, fake, phone_store) -> None:
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)
    loop = asyncio.get_running_loop()
    began = loop.time()
    await service.stop()
    took = loop.time() - began
    assert service.STOP_WAIT_S <= took < service.STOP_WAIT_S + 0.3
    assert not service._tasks
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    # Left for the next boot, which ends it as interrupted.
    assert row["outcome"] is None


async def test_no_call_is_admitted_while_the_stop_waits(running, fake) -> None:
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)
    stopping = asyncio.create_task(service.stop())
    await wait_for(lambda: verbs(fake, "hangup"))
    service.on_incoming(Incoming("live_b", "+31612345678", LINE))
    await stopping
    assert [s for s, _ in verbs(fake, "accept")] == ["live_a"]
    assert verbs(fake, "reject") == []


async def test_a_call_admitted_while_the_key_is_read_is_not_accepted(
        running, fake) -> None:
    configure, op, vault, clock = running
    vault.gate = threading.Event()
    vault.gate_field = "credential"
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: "credential" in vault.reads)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0.05)
    vault.gate.set()
    await stopping
    assert fake.requests == []
    assert op.lines == []


async def test_an_admission_that_sees_the_stop_sends_nothing(
        running, fake, phone_store) -> None:
    assert await service.key() == KEY
    # The webhook admitted the call just before the stop was declared.
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    service.begin_stop()
    await idle()
    assert fake.requests == []
    # Refused before the call was even looked at: no row either.
    with phone_store.transaction() as conn:
        assert pstore.recent_calls(conn) == []


async def test_an_accept_that_lands_after_the_hang_up_is_hung_up_again(
        running, fake) -> None:
    fake.hang_for.add("accept")
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept"))
    await service.stop()
    # Once at the stop, once more before the call's task is cancelled.
    assert [s for s, _ in verbs(fake, "hangup")] == ["live_a", "live_a"]
    assert not service._tasks


async def test_redeliver_leaves_a_running_call_of_this_process_alone(
        running, fake, phone_store) -> None:
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)
    await service.redeliver()
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert row["outcome"] is None


async def test_a_caller_who_hangs_up_during_the_stop_keeps_the_outcome(
        running, fake, phone_store) -> None:
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)
    service.begin_stop()
    await fake.close("live_a", "remote_hangup")
    await wait_for(lambda: not service._call_tasks)
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert row["outcome"] == "caller_hung_up"
    assert verbs(fake, "hangup") == []


async def test_a_transfer_still_pending_when_the_stop_cuts_it_is_interrupted(
        running, fake, phone_store, monkeypatch) -> None:
    monkeypatch.setattr(service, "STOP_WAIT_S", 2.0)
    monkeypatch.setenv("PHONE_TRANSFER_NUMBER", "+31687654321")
    settings.reset()
    sid = "live_a"
    service.on_incoming(Incoming(sid, "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)
    await fake.emit(sid, {"type": "session.delegation.created",
                          "delegation": {"id": "d1", "type": "delegation"}})
    await fake.emit(sid, {**tool_call_event("transfer", {"why": "asked"}),
                          "delegation_id": "d1"})
    await wait_for(lambda: verbs(fake, "refer"))

    async def provider() -> None:
        # The session closes only after the stop's hang-up.
        await wait_for(lambda: verbs(fake, "hangup"))
        await fake.close(sid, "close_requested")
    closer = asyncio.create_task(provider())
    await service.stop()
    await closer
    assert len(verbs(fake, "refer")) == 1
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert row["outcome"] == "interrupted"


async def test_finished_calls_are_forgotten(running, fake) -> None:
    configure, op, vault, clock = running
    configure(timing=QUICK)
    for sid in ("live_a", "live_b", "live_c"):
        service.on_incoming(Incoming(sid, "+31612345678", LINE))
        await idle()
    assert len(verbs(fake, "accept")) == 3
    assert service._ours == set()


async def test_a_running_call_is_remembered(running, fake,
                                            phone_store) -> None:
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert service._ours == {row["id"]}


async def test_a_refused_hang_up_does_not_mark_the_call_cut(
        running, fake, phone_store, monkeypatch) -> None:
    monkeypatch.setattr(service, "STOP_WAIT_S", 2.0)
    fake.status_for["hangup"] = 500
    sid = "live_a"
    service.on_incoming(Incoming(sid, "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)

    async def caller() -> None:
        # The provider refused the hang-up; the caller then hangs up.
        await wait_for(lambda: verbs(fake, "hangup"))
        await asyncio.sleep(0.05)
        await fake.close(sid, "remote_hangup")
    closer = asyncio.create_task(caller())
    await service.stop()
    await closer
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert row["outcome"] == "caller_hung_up"


async def test_a_close_seen_before_the_hang_up_answer_is_interrupted(
        running, fake, phone_store, monkeypatch) -> None:
    monkeypatch.setattr(service, "STOP_WAIT_S", 2.0)
    answer = asyncio.Event()
    fake.hold_for["hangup"] = answer
    sid = "live_a"
    service.on_incoming(Incoming(sid, "+31612345678", LINE))
    await wait_for(lambda: verbs(fake, "accept") and fake.attach_count)

    async def provider() -> None:
        # The session closes before the hang-up request is answered.
        await wait_for(lambda: verbs(fake, "hangup"))
        await fake.close(sid, "close_requested")
        await wait_for(lambda: not service._call_tasks)
        answer.set()
    closer = asyncio.create_task(provider())
    await service.stop()
    await closer
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert row["outcome"] == "interrupted"
