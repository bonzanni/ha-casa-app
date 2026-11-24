"""The running phone service: admission, the vault reads, the call slots, the
missing-login line, the stop, and the route that exists only with the line on.

The service side is the local fake; stores are real files in a temporary
directory. Numbers and vault items are invented."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import threading
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import logins
from live_voice.session import Timing
from op_fake import fresh_logins, install, item
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
VOICE = "openai_voice"
SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef" * 2).decode()
#: What the check finds while no item holds the login.
NOT_FOUND = logins.Check(ok=False, problem="item_not_found", field="",
                         item_is_default=True, service=VOICE)
NO_KEY_LINE = ("A phone call came in but could not be answered. The vault "
               "has no item titled 'OpenAI Voice' for OpenAI voice.")

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
    monkeypatch.delenv("PHONE_LINE_NUMBER", raising=False)
    monkeypatch.delenv("LIVE_VOICES", raising=False)
    fresh_logins(monkeypatch, tmp_path)
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
    """Stands in for the read of one value of the login from a vault item:
    values per field, an optional gate a read blocks on (a real thread
    blocks, as ``op`` would), and a count."""

    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values
        self.reads: list[str] = []
        self.gate: threading.Event | None = None
        self.gate_field: str | None = None
        #: What the login check answers.
        self.found = NOT_FOUND

    def __call__(self, item: str, field: str) -> str | None:
        # Nothing is chosen in these tests: the item under the default title.
        assert item == "OpenAI Voice"
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
                          read_secret=vault, check=lambda: vault.found,
                          http=http, timing=timing,
                          clock=clock, base_url=fake.base_url)

    configure()
    try:
        yield configure, op, vault, clock
    finally:
        if vault.gate is not None:
            vault.gate.set()
        await service.stop()


@pytest.fixture
async def vaulted(fake, http, phone_store, monkeypatch):
    """The service reading its login the way Casa runs it, from a fake
    ``op`` holding one complete item under the default title; stops it."""
    monkeypatch.setattr(service, "STOP_WAIT_S", 0.5)
    op = Operator()
    clock = Clock()
    vault = install(monkeypatch, item(
        "oai1", "OpenAI Voice", "API_CREDENTIAL", credential=KEY,
        webhook_secret=SECRET))
    service.configure(store=phone_store, contacts=lambda: None, notify=op,
                      http=http, timing=LONG, clock=clock,
                      base_url=fake.base_url)
    try:
        yield op, vault, clock
    finally:
        await service.stop()


def signed(body: bytes, secret: str, webhook_id: str) -> dict:
    """Standard Webhooks headers for *body*, signed now with *secret*."""
    ts = str(int(time.time()))
    mac = hmac.new(base64.b64decode(secret.removeprefix("whsec_")),
                   f"{webhook_id}.{ts}.".encode() + body, hashlib.sha256)
    return {"webhook-id": webhook_id, "webhook-timestamp": ts,
            "webhook-signature": "v1," + base64.b64encode(
                mac.digest()).decode(),
            "content-type": "application/json"}


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


def test_active_store_only_while_configured_and_available(
        tmp_path, phone_store, monkeypatch) -> None:
    monkeypatch.setattr(service, "_store", None)
    assert service.active_store() is None
    monkeypatch.setattr(service, "_store",
                        PhoneStore(str(tmp_path / "never-opened")))
    assert service.active_store() is None
    monkeypatch.setattr(service, "_store", phone_store)
    assert service.active_store() is phone_store


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
    assert op.lines == [NO_KEY_LINE]
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
    # Without a bus: the first line (who, the full number, when, how it
    # ended), then the note that the summary did not go.
    assert len(op.lines) == 2
    assert op.lines[0].startswith("Unknown caller · +31612345678 · ")
    assert op.lines[0].endswith(" · the caller hung up")
    assert op.lines[1] == service.NOT_DELIVERED
    with phone_store.transaction() as conn:
        assert pstore.get_call(conn, "c1")["delivered"] == 0
    # An unknown id is not an error.
    await service.finish("nope")


async def test_an_accept_refused_call_is_reported_end_to_end(
        running, fake, phone_store) -> None:
    """From the webhook's callback to the operator: a call whose accept the
    provider refuses is recorded and its first line sent (no bus here, so
    the note that the summary did not go follows)."""
    configure, op, vault, clock = running
    fake.status_for["accept"] = 500
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await idle()
    with phone_store.transaction() as conn:
        [row] = pstore.recent_calls(conn)
    assert row["outcome"] == "accept_failed" and row["headline_sent"] == 1
    assert len(op.lines) == 2
    assert op.lines[0].startswith("Unknown caller · +31612345678 · ")
    assert op.lines[0].endswith(" · it could not be answered")
    assert op.lines[1] == service.NOT_DELIVERED


async def test_a_first_line_retried_after_midnight_carries_the_date(
        running, phone_store, monkeypatch) -> None:
    from datetime import datetime, timedelta

    from timekeeping import resolve_tz
    configure, op, vault, clock = running
    monkeypatch.setattr(service, "HEADLINE_RETRY_S", (0.01, 0.01))
    day = datetime(2026, 6, 15, 23, 59, 50, tzinfo=resolve_tz())
    started = day.timestamp()
    # The wall clock at each try: the same day, then the next one.
    tries = [started + 5, (day + timedelta(seconds=25)).timestamp()]
    monkeypatch.setattr(service, "_now",
                        lambda: tries.pop(0) if len(tries) > 1 else tries[0])
    answers = [False, True]
    delivered: list[str] = []

    async def flaky(text) -> bool:
        op.lines.append(str(text))
        ok = answers.pop(0) if answers else True
        if ok:
            delivered.append(str(text))
        return ok
    service._notify = flaky
    with phone_store.transaction(write=True) as conn:
        pstore.insert_call(conn, {
            "id": "c1", "session_id": "live_x", "started_at": started,
            "number": "+31612345678", "name": "", "caller_class": "unknown",
            "language": "nl", "returned": 0})
        pstore.finish_call(conn, "c1", ended_at=started + 30,
                           outcome="caller_hung_up", actions=[], record=None)
    await service.finish("c1")
    assert op.lines[0] == ("Unknown caller · +31612345678 · 23:59 · "
                           "the caller hung up")
    assert delivered[0] == ("Unknown caller · +31612345678 · "
                            "2026-06-15 23:59 · the caller hung up")


async def test_purge_never_raises(running, phone_store, tmp_path) -> None:
    configure, op, vault, clock = running
    with phone_store.transaction(write=True) as conn:
        pstore.insert_call(conn, {"id": "c1", "session_id": "live_x"})
        pstore.put_transcript(conn, "c1", "Caller: hi", 1.0)
    await service.purge()
    with phone_store.transaction() as conn:
        # Read as of before its expiry: gone because it was purged.
        assert pstore.transcript(conn, "c1", 0.0) is None
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
        text = pstore.transcript(conn, row["id"], row["ended_at"])
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


# --- the login, read the way Casa runs it ---------------------------------

async def test_choosing_another_item_replaces_both_cached_values(
        vaulted) -> None:
    op, vault, clock = vaulted
    assert await service.key() == KEY and await service.secret() == SECRET
    before = len(vault.calls)
    assert await service.key() == KEY and await service.secret() == SECRET
    assert len(vault.calls) == before             # both served from memory

    vault.items.append(item("oai2", "OpenAI (second project)",
                            "API_CREDENTIAL", credential="sk-test-second",
                            webhook_secret="whsec_second"))
    logins.choose(VOICE, "oai2")
    assert await service.key() == "sk-test-second"
    assert await service.secret() == "whsec_second"
    assert {ref for ref, _ in vault.reads()[2:]} == {"oai2"}

    # The same item taken again after its key was replaced: what was read
    # from it before is not served any more.
    vault.get("oai2")["fields"]["credential"] = "sk-test-rotated"
    assert await service.key() == "sk-test-second"
    logins.choose(VOICE, "oai2")
    assert await service.key() == "sk-test-rotated"

    # Without the choice, the item under the default title again.
    logins.forget(VOICE)
    assert await service.key() == KEY and await service.secret() == SECRET


async def test_a_read_begun_before_a_choice_is_not_kept_for_the_new_one(
        vaulted) -> None:
    op, vault, clock = vaulted
    vault.items.append(item("oai2", "OpenAI (second project)",
                            "API_CREDENTIAL", credential="sk-test-second",
                            webhook_secret="whsec_second"))
    gate = threading.Event()

    def hold(args: list[str]) -> None:
        # The first read of the default item waits in its thread.
        if "--fields" in args and args[3] == "OpenAI Voice":
            gate.wait(5)
    vault.on_call = hold
    early = asyncio.ensure_future(service.key())
    await wait_for(lambda: vault.reads())
    logins.choose(VOICE, "oai2")
    assert await service.key() == "sk-test-second"
    gate.set()
    await early
    assert await service.key() == "sk-test-second"
    assert await service.secret() == "whsec_second"


class HeldVault:
    """Stands in for the read of one value of the login. A read takes the
    value the vault holds when it is asked; the first ``hold`` reads then
    wait, each in its own thread, until the test lets that one go."""

    def __init__(self, hold: int, **values: str) -> None:
        self.values = values
        self.reads: list[tuple[str, str]] = []
        self.entered = [threading.Event() for _ in range(hold)]
        self.release = [threading.Event() for _ in range(hold)]
        self._lock = threading.Lock()

    def __call__(self, item: str, field: str) -> str | None:
        with self._lock:
            n = len(self.reads)
            self.reads.append((item, field))
            value = self.values.get(field)
        if n < len(self.entered):
            self.entered[n].set()
            self.release[n].wait(10)
        return value

    async def wait_entered(self, n: int) -> None:
        assert await asyncio.to_thread(self.entered[n].wait, 10)


async def test_a_read_under_way_does_not_undo_a_login_chosen_again(
        running) -> None:
    configure, op, vault, clock = running
    held = HeldVault(1, credential=KEY, webhook_secret="whsec_old")
    service._read_secret = held
    logins.choose(VOICE, "oai1")

    # A read of the secret is under way when the secret is replaced in the
    # same item and that item is taken again.
    early = asyncio.ensure_future(service.secret())
    await held.wait_entered(0)
    held.values.update(webhook_secret="whsec_new",
                       credential="sk-test-rotated")
    logins.choose(VOICE, "oai1")
    assert await service.secret() == "whsec_new"

    # The read begun before then ends: what it found is thrown away, for
    # the one who waited on it and for everyone after.
    held.release[0].set()
    assert await early == "whsec_new"
    clock.t += service.CACHE_S - 1
    assert await service.secret() == "whsec_new"
    assert await service.key() == "sk-test-rotated"
    # One read begun before the choice, one after it, and the key once.
    assert held.reads == [("oai1", "webhook_secret"),
                          ("oai1", "webhook_secret"), ("oai1", "credential")]


async def test_a_read_overtaken_twice_gives_nothing_and_reads_no_third_time(
        running) -> None:
    configure, op, vault, clock = running
    held = HeldVault(2, credential=KEY, webhook_secret="whsec_one")
    service._read_secret = held
    logins.choose(VOICE, "oai1")

    waiting = asyncio.ensure_future(service.secret())
    await held.wait_entered(0)
    logins.choose(VOICE, "oai1")
    held.release[0].set()
    # Read once more under the new choice, which is overtaken as well.
    await held.wait_entered(1)
    held.values["webhook_secret"] = "whsec_three"
    logins.choose(VOICE, "oai1")
    held.release[1].set()
    # Not a value read under a choice that no longer holds, and no loop.
    assert await waiting is None
    assert len(held.reads) == 2
    assert await service.secret() == "whsec_three"
    assert len(held.reads) == 3


async def test_a_call_without_a_key_tells_the_operator_what_is_wrong(
        vaulted, fake) -> None:
    op, vault, clock = vaulted
    vault.get("oai1")["fields"]["credential"] = ""
    service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    await idle()
    assert fake.requests == []
    assert op.lines == [
        "A phone call came in but could not be answered. The API key in the "
        "vault item for OpenAI voice is empty."]


@pytest.mark.parametrize("state", [
    "no-key", "empty-key", "no-secret", "empty-secret", "chosen-gone",
    "default-gone", "vault-down", "choice-unreadable", "no-vault"])
async def test_the_missing_login_line_holds_no_label(
        vaulted, fake, monkeypatch, state: str) -> None:
    # The operator may answer Casa's line, and the quoted line then reaches
    # the assistant: in every state it names values by Casa's names only.
    op, vault, clock = vaulted
    monkeypatch.setitem(logins.SERVICES, VOICE, dataclasses.replace(
        logins.SERVICES[VOICE], fields={
            "credential": ("zzlabel-key", "zzlabel-apikey"),
            "webhook_secret": ("zzlabel-sec", "zzlabel-whsec")}))
    fields = {"zzlabel-apikey": "ZZVALUE-key", "zzlabel-whsec": "ZZVALUE-sec"}
    if state == "no-key":
        del fields["zzlabel-apikey"]
    elif state == "empty-key":
        fields["zzlabel-apikey"] = ""
    elif state == "no-secret":
        del fields["zzlabel-whsec"]
    elif state == "empty-secret":
        fields["zzlabel-whsec"] = ""
    vault.items = [] if state == "default-gone" else [
        item("oai1", "OpenAI Voice", "API_CREDENTIAL", **fields)]
    if state == "chosen-gone":
        logins.choose(VOICE, "gone1")
    elif state == "vault-down":
        vault.down = True
    elif state == "choice-unreadable":
        Path(logins.PATH).write_text("{ damaged")
        monkeypatch.setattr(logins, "_choices", None)
    elif state == "no-vault":
        monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT")
    if state.endswith("-key"):
        service.on_incoming(Incoming("live_a", "+31612345678", LINE))
    else:
        assert await service._webhook_secret() is None
    await idle()
    assert fake.requests == []
    [line] = op.lines
    assert "zzlabel" not in line.lower() and "ZZVALUE" not in line
    assert "`" not in line and "field" not in line
    # Casa's own key for a value is an accepted vault label too.
    assert all(key not in line.lower() for key in logins.SERVICES[VOICE].fields)
    if state == "empty-secret":
        assert line.endswith("The webhook signing secret in the vault item "
                             "for OpenAI voice is empty.")
    assert "could not be read from the vault" not in line


async def test_a_deleted_login_answers_503_and_tells_once_an_hour(
        vaulted, fake) -> None:
    op, vault, clock = vaulted
    vault.items.append(item("oai2", "OpenAI (second project)",
                            "API_CREDENTIAL", credential="sk-test-second",
                            webhook_secret=SECRET))
    logins.choose(VOICE, "oai2")
    app = web.Application()
    assert service.add_routes(app, default_region=lambda: "NL") is True
    body = json.dumps({"type": "live.session.ended", "data": {}}).encode()
    async with TestClient(TestServer(app)) as client:
        async def post(webhook_id: str) -> int:
            resp = await client.post(PHONE_WEBHOOK_PATH, data=body,
                                     headers=signed(body, SECRET, webhook_id))
            await idle()
            return resp.status

        # A successful start: the signed request is taken.
        assert await post("wh_1") == 200
        assert await service.key() == "sk-test-second"
        assert op.lines == []

        # The chosen item is deleted; what was read stays for a while.
        vault.remove("oai2")
        clock.t += service.CACHE_S + 1
        assert await post("wh_2") == 503
        gone = ("The phone line cannot check incoming calls. The vault item "
                "chosen for OpenAI voice is no longer in the vault.")
        assert op.lines == [gone]
        # Within the hour: refused the same way, and no second line.
        clock.t += 1800
        assert await post("wh_3") == 503
        service.on_incoming(Incoming("live_a", "+31612345678", LINE))
        await idle()
        assert op.lines == [gone]
        clock.t += 1801
        assert await post("wh_4") == 503
        assert op.lines == [gone, gone]
    # The complete item under the default title was never read instead.
    assert "OpenAI Voice" not in {c[3] for c in vault.calls
                                  if c[2] == "get"}
    assert fake.requests == []


async def test_the_boot_log_says_whether_a_returned_call_is_recognised(
        vaulted, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="phone.service"):
        service.start_boot_check("https://casa.example")
        await idle()
    assert "phone_line_number is not set" in caplog.text
    caplog.clear()

    monkeypatch.setenv("PHONE_LINE_NUMBER", "+31201234567")
    settings.reset()
    with caplog.at_level(logging.INFO, logger="phone.service"):
        service.start_boot_check("https://casa.example")
        await idle()
    assert "phone_line_number is not set" not in caplog.text
    assert "4567" in caplog.text
    assert "1234567" not in caplog.text


async def test_a_damaged_choice_file_answers_503_and_tells_the_operator(
        vaulted, fake, monkeypatch, tmp_path) -> None:
    op, vault, clock = vaulted
    # The file of choices is found damaged; the item under the default
    # title is complete and must not be read in its place.
    Path(logins.PATH).write_text("{ damaged")
    monkeypatch.setattr(logins, "_choices", None)
    app = web.Application()
    assert service.add_routes(app, default_region=lambda: "NL") is True
    body = json.dumps({"type": "live.session.ended", "data": {}}).encode()
    async with TestClient(TestServer(app)) as client:
        async def post(webhook_id: str) -> int:
            resp = await client.post(PHONE_WEBHOOK_PATH, data=body,
                                     headers=signed(body, SECRET, webhook_id))
            await idle()
            return resp.status

        assert await post("wh_1") == 503
        unreadable = (
            "The phone line cannot check incoming calls. Casa can no longer "
            "read which vault item was chosen for OpenAI voice; ask the "
            "assistant to connect it again.")
        assert op.lines == [unreadable]
        # Within the hour: refused the same way, a call is not answered,
        # and no second line.
        clock.t += 1800
        assert await post("wh_2") == 503
        service.on_incoming(Incoming("live_a", "+31612345678", LINE))
        await idle()
        assert op.lines == [unreadable]
        assert vault.calls == [] and fake.requests == []

        # The item is taken again: the line works, with that item.
        logins.choose(VOICE, "oai1")
        assert await post("wh_3") == 200
        assert await service.key() == KEY
    assert {c[3] for c in vault.calls} == {"oai1"}


async def test_the_boot_log_says_what_is_wrong_with_the_login(
        vaulted, caplog: pytest.LogCaptureFixture) -> None:
    op, vault, clock = vaulted
    with caplog.at_level(logging.INFO, logger="phone.service"):
        service.start_boot_check("https://casa.example")
        await idle()
    assert f"webhook at https://casa.example{PHONE_WEBHOOK_PATH}" \
        in caplog.text
    caplog.clear()

    del vault.get("oai1")["fields"]["webhook_secret"]
    clock.t += service.CACHE_S + 1
    with caplog.at_level(logging.INFO, logger="phone.service"):
        service.start_boot_check("https://casa.example")
        await idle()
    assert ("calls are not answered: The vault item for OpenAI voice has no "
            "webhook signing secret.") in caplog.text
    assert KEY not in caplog.text and op.lines == []
