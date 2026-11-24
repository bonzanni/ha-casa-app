"""One phone call, end to end: lookup, accept, the live session, the outcome.

The service side is the local fake (the real client talks to it); the phone
and contacts stores are real files in a temporary directory. People, numbers
and voice ids are invented."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path

import aiohttp
import pytest

from contacts import store as cstore
from contacts.store import ContactsStore, Person
from live_voice.api import LiveApi
from live_voice.session import Timing
from phone import controller
from phone import settings
from phone import store as pstore
from phone.controller import Deps
from phone.settings import Options
from phone.store import PhoneStore
from phone.webhook import Incoming
from phone_fakes import FakeOpenAI, tool_call_event

DEFAULTS = (Path(__file__).resolve().parents[1]
            / "casa" / "rootfs" / "opt" / "casa" / "defaults")
KEY = "sk-test-not-a-real-key"
LINE = "+31201234567"          # the line the calls come in on
PRIORITY = "+31612345678"
TRANSFER_TO = "+31687654321"
STORED_NL_VOICE = "voice_test_nl"
NOW = 1_800_000_000.0

TIMING = Timing(first_speech_s=3.0, quiet_s=5.0, silence_s=5.0,
                max_call_s=8.0, reconnect_s=1.0, decision_timeout_s=0.5,
                undecided_silence_s=1.0, goodbye_grace_s=0.3,
                speak_margin_s=0.0, pending_s=2.0)
OPTIONS = Options(enabled=True, transfer_number=TRANSFER_TO,
                  owner_name="Sam Owner", seed_voices={}, api_host="",
                  line_number=LINE)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    root = tmp_path / "config"
    root.mkdir()
    monkeypatch.setenv("CASA_CONFIG_DIR", str(root))
    monkeypatch.setenv("CASA_DEFAULTS_DIR", str(DEFAULTS))
    monkeypatch.delenv("LIVE_VOICES", raising=False)
    monkeypatch.delenv("PHONE_TRANSFER_NUMBER", raising=False)
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
    with store.transaction(write=True) as conn:
        pstore.set_voice(conn, "nl", STORED_NL_VOICE)
    return store


@pytest.fixture
def contacts(tmp_path) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open(), store.unavailable_reason
    person = Person(source="icloud", uid="A", name="Bea Sample",
                    first_name="Bea", org="", note="", numbers=(PRIORITY,),
                    groups=(), related=())
    with store.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", [person])
        cstore.set_mark(conn, "icloud", "A", "priority", "1", "operator")
    return store


class Operator:
    """The operator's side of ``notify`` and ``finish``."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.finished: list[str] = []

    async def notify(self, text) -> bool:
        self.lines.append(str(text))
        return True

    async def finish(self, call_id: str) -> None:
        self.finished.append(call_id)


def deps(fake, http, store, *, contacts=None, op: Operator,
         **extra) -> Deps:
    return Deps(api=LiveApi(key=KEY, http=http, base_url=fake.base_url),
                store=store, contacts=lambda: contacts, options=OPTIONS,
                default_region="NL", notify=op.notify, finish=op.finish,
                timing=TIMING, now=lambda: NOW, **extra)


def accepted(fake, sid: str) -> list[dict]:
    return [b["session"] for v, s, b in fake.requests
            if v == "accept" and s == sid]


def tool_names(session: dict) -> set[str]:
    return {t["name"] for t in session["delegation"]["responses"]["tools"]}


def calls(store: PhoneStore) -> list[dict]:
    with store.transaction() as conn:
        return pstore.recent_calls(conn, 50)


async def wait_for(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_known_priority_caller_end_to_end(fake, http, phone_store,
                                                 contacts) -> None:
    op = Operator()
    sid = "live_known"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, contacts=contacts, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    session = accepted(fake, sid)[0]
    assert "Bea" in session["instructions"]
    assert session["audio"]["output"]["voice"] == {"id": STORED_NL_VOICE}
    assert "transfer" in tool_names(session)
    await fake.emit(sid, {"type": "session.output_transcript.delta",
                          "delta": "Hello Bea", "start_ms": 0, "end_ms": 400})
    await fake.emit(sid, {"type": "session.input_transcript.delta",
                          "delta": "Hi, just a message", "start_ms": 500,
                          "end_ms": 900})
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)

    [row] = calls(phone_store)
    assert row["session_id"] == sid
    assert row["outcome"] == "caller_hung_up"
    assert row["caller_class"] == "priority"
    assert row["name"] == "Bea Sample"
    assert row["language"] == "nl"
    assert row["returned"] == 0
    with phone_store.transaction() as conn:
        text = pstore.transcript(conn, row["id"], row["ended_at"])
    assert text == "Assistant: Hello Bea\nCaller: Hi, just a message"
    with phone_store.transaction() as conn:
        [(expires,)] = conn.execute(
            "SELECT expires_at FROM transcripts").fetchall()
    assert expires == 1_800_000_000.0 + 30 * 24 * 3600
    assert op.finished == [row["id"]]
    assert op.lines == []


async def test_the_delays_of_a_call_are_logged_once_as_numbers(
        fake, http, phone_store, contacts, caplog) -> None:
    op = Operator()
    sid = "live_delays"
    with caplog.at_level(logging.INFO, logger="phone.controller"):
        task = asyncio.create_task(controller.handle_call(
            Incoming(sid, PRIORITY, LINE),
            deps(fake, http, phone_store, contacts=contacts, op=op)))
        await wait_for(lambda: accepted(fake, sid))
        for kind, text, start_ms, end_ms in (
                ("output", "Hello Bea", 0, 400),
                ("input", "Hi, just a message", 600, 900),
                ("output", "Go ahead", 1100, 1500),
                ("input", "Call me back", 2000, 2600),
                ("output", "I will pass it on", 3200, 3900)):
            await fake.emit(sid, {"type": f"session.{kind}_transcript.delta",
                                  "delta": text, "start_ms": start_ms,
                                  "end_ms": end_ms})
        await fake.close(sid, "remote_hangup")
        await asyncio.wait_for(task, 5)
    found = [r.getMessage() for r in caplog.records
             if r.name == "phone.controller" and "reply gap" in r.getMessage()]
    assert len(found) == 1
    [line] = found
    assert "reply gap median 400 ms, max 600 ms" in line
    assert "first speech" in line
    # Numbers only: nothing said in the call, nobody's name or number.
    for word in ("Bea", "message", "Call me back", PRIORITY[-4:]):
        assert word not in line
    # For tuning only: neither the call's record nor the operator's chat.
    [row] = calls(phone_store)
    assert not any("gap" in key or "speech" in key for key in row)
    assert op.lines == []


async def test_unknown_number_without_contacts_store(fake, http,
                                                     phone_store) -> None:
    op = Operator()
    sid = "live_unknown"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, "+39201110099", LINE),
        deps(fake, http, phone_store, contacts=None, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    [row] = calls(phone_store)
    assert row["caller_class"] == "unknown"
    assert row["language"] == "it"
    assert row["name"] == ""
    assert "Italian" in accepted(fake, sid)[0]["instructions"]
    assert op.finished == [row["id"]]


# The To header of a real call carries no number, so only the line's own
# number as the caller marks a call that came back.
@pytest.mark.parametrize("to_number", ["", LINE, "+31209999999"])
async def test_returned_call_gets_no_identity(fake, http, phone_store,
                                              contacts, monkeypatch,
                                              to_number) -> None:
    # A call of the priority contact that was put through seconds earlier.
    with phone_store.transaction(write=True) as conn:
        pstore.insert_call(conn, {
            "id": "earlier", "session_id": "live_earlier",
            "started_at": 1_799_999_990.0, "number": PRIORITY,
            "name": "Bea Sample", "caller_class": "priority",
            "language": "nl", "returned": 0})
        pstore.finish_call(conn, "earlier", ended_at=1_799_999_995.0,
                           outcome="transferred", actions=[], record=None)
    looked_up: list = []
    monkeypatch.setattr(controller, "lookup",
                        lambda *a, **k: looked_up.append(a))
    op = Operator()
    sid = "live_back"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, LINE, to_number),
        deps(fake, http, phone_store, contacts=contacts, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    session = accepted(fake, sid)[0]
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)

    assert looked_up == []
    assert "Bea" not in session["instructions"]
    assert "Sample" not in session["instructions"]
    names = tool_names(session)
    assert "transfer" not in names
    assert "alert_operator" in names
    row = next(r for r in calls(phone_store) if r["session_id"] == sid)
    assert row["caller_class"] == "unknown"
    assert row["returned"] == 1
    assert row["name"] == ""
    assert row["language"] == "en"


async def test_without_the_line_number_a_call_from_it_is_ordinary(
        fake, http, phone_store, contacts, monkeypatch) -> None:
    looked_up: list = []
    real = controller.lookup

    def spy(*args, **kwargs):
        looked_up.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(controller, "lookup", spy)
    op = Operator()
    sid = "live_unset"
    d = dataclasses.replace(
        deps(fake, http, phone_store, contacts=contacts, op=op),
        options=dataclasses.replace(OPTIONS, line_number=""))
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, LINE, LINE), d))
    await wait_for(lambda: accepted(fake, sid))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    assert looked_up == [LINE]
    row = next(r for r in calls(phone_store) if r["session_id"] == sid)
    assert row["returned"] == 0


async def test_unavailable_phone_store_still_answers(fake, http, tmp_path,
                                                     contacts) -> None:
    store = PhoneStore(str(tmp_path / "never-opened"))
    op = Operator()
    sid = "live_nostore"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, store, contacts=contacts, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    session = accepted(fake, sid)[0]
    # Seed voices and default models; every grant off.
    assert session["model"] == settings.DEFAULTS["voice_model"]
    assert "transfer" not in tool_names(session)
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    assert op.finished == []
    [line] = op.lines
    # Who, the full number, when, how it ended; then the note.
    assert line.startswith(
        f"Bea Sample · {PRIORITY} · {controller.when_words(NOW)} · ")
    assert "the caller hung up" in line
    assert line.endswith(controller.UNRECORDED_NOTE)
    assert not (tmp_path / "never-opened" / "phone.db").exists()


async def test_accept_refused(fake, http, phone_store) -> None:
    fake.status_for["accept"] = 500
    op = Operator()
    sid = "live_refused"
    await asyncio.wait_for(controller.handle_call(
        Incoming(sid, PRIORITY, LINE), deps(fake, http, phone_store, op=op)),
        5)
    [row] = calls(phone_store)
    assert row["outcome"] == "accept_failed"
    assert op.finished == [row["id"]]
    assert fake.attach_count == 0
    with phone_store.transaction() as conn:
        assert pstore.transcript(conn, row["id"], 0.0) is None


async def test_transfer_then_closed_is_transferred(fake, http, phone_store,
                                                   contacts) -> None:
    op = Operator()
    sid = "live_transfer"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, contacts=contacts, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.emit(sid, {"type": "session.output_transcript.delta",
                          "delta": "Hello", "start_ms": 0, "end_ms": 300})
    await fake.emit(sid, {"type": "session.delegation.created",
                          "delegation": {"id": "d1", "type": "delegation"}})
    await fake.emit(sid, {**tool_call_event("transfer", {"why": "asked"}),
                          "delegation_id": "d1"})
    await wait_for(lambda: any(v == "refer" for v, _, _ in fake.requests))
    refer = next(b for v, _, b in fake.requests if v == "refer")
    assert refer == {"target_uri": "tel:" + TRANSFER_TO}
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    [row] = calls(phone_store)
    assert row["outcome"] == "transferred"
    assert any(a["ability"] == "transfer" and a["status"] == "done"
               for a in row["actions"])
    # The operator was told who is being put through, and from which number.
    assert op.lines == [f"Putting through: Bea Sample ({PRIORITY})"]
    assert op.finished == [row["id"]]


async def test_session_that_raises_is_an_error(fake, http, phone_store) -> None:
    class Broken:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self):
            raise RuntimeError("boom")

    op = Operator()
    sid = "live_broken"
    await asyncio.wait_for(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, op=op, make_session=Broken)), 5)
    assert [s for v, s, _ in fake.requests if v == "hangup"] == [sid]
    [row] = calls(phone_store)
    assert row["outcome"] == "error"
    assert op.finished == [row["id"]]


async def test_clock_ending_is_ended_by_casa(fake, http, phone_store) -> None:
    op = Operator()
    sid = "live_silent"
    quick = Timing(first_speech_s=0.2, quiet_s=5.0, silence_s=5.0,
                   max_call_s=8.0, reconnect_s=1.0, decision_timeout_s=0.5,
                   undecided_silence_s=1.0, goodbye_grace_s=0.3,
                   speak_margin_s=0.0, pending_s=2.0)
    d = deps(fake, http, phone_store, op=op)
    d = Deps(**{**d.__dict__, "timing": quick})
    await asyncio.wait_for(controller.handle_call(
        Incoming(sid, "", LINE), d), 5)
    [row] = calls(phone_store)
    assert row["outcome"] == "ended_by_casa"
    assert row["number"] == ""


def test_the_headline_says_who_which_number_when_and_how_it_ended() -> None:
    from datetime import datetime

    from timekeeping import resolve_tz
    # Noon local time, so a minute earlier is the same day whatever the
    # wall clock says.
    now = datetime(2026, 6, 15, 12, 0, tzinfo=resolve_tz()).timestamp()
    row = {"name": "", "number": "+31612345678", "caller_class": "unknown",
           "outcome": "caller_hung_up", "started_at": now - 60,
           "ended_at": now - 18, "returned": 0}

    def line(**over) -> str:
        return controller.headline({**row, **over}, now=now)
    assert controller.when_words(now - 60, now) == "11:59"
    assert line() == (
        "Unknown caller · +31612345678 · 11:59 · the caller hung up")
    assert line(number="").startswith(
        "Unknown caller · number withheld · 11:59 · ")
    assert line(name="Ann Example").startswith(
        "Ann Example · +31612345678 · 11:59 · ")
    # A call back from a transfer carries the line's own number: not shown.
    back = line(number=LINE, returned=1)
    assert back == ("A caller who came back after being put through · 11:59 · "
                    "the caller hung up")
    # A call of an earlier day carries its date.
    assert controller.when_words(now - 3 * 24 * 3600, now) \
        == "2026-06-12 12:00"
    assert line(started_at=now - 3 * 24 * 3600).startswith(
        "Unknown caller · +31612345678 · 2026-06-12 12:00 · ")


def test_a_time_out_of_range_leaves_the_time_out() -> None:
    row = {"name": "", "number": "+31612345678", "outcome": "caller_hung_up",
           "started_at": 1e300, "returned": 0}
    assert controller.when_words(1e300) == ""
    assert controller.headline(row) == (
        "Unknown caller · +31612345678 · the caller hung up")


def test_the_log_shows_a_number_by_its_last_four_digits() -> None:
    assert controller.number_tail("+31612345678") == "…5678"
    assert controller.number_tail("") == "withheld"
    assert controller.number_words("+31612345678") == "+31612345678"


async def test_unanswered_accept_is_hung_up(fake, http, phone_store) -> None:
    fake.hang_for.add("accept")
    op = Operator()
    sid = "live_uncertain"
    d = deps(fake, http, phone_store, op=op)
    d = Deps(**{**d.__dict__, "api": LiveApi(key=KEY, http=http,
                                             base_url=fake.base_url,
                                             timeout_s=0.2)})
    await asyncio.wait_for(controller.handle_call(
        Incoming(sid, PRIORITY, LINE), d), 5)
    assert [s for v, s, _ in fake.requests if v == "hangup"] == [sid]
    [row] = calls(phone_store)
    assert row["outcome"] == "accept_failed"
    assert op.finished == [row["id"]]


async def test_refused_accept_is_not_hung_up(fake, http, phone_store) -> None:
    fake.status_for["accept"] = 400
    op = Operator()
    await asyncio.wait_for(controller.handle_call(
        Incoming("live_no", PRIORITY, LINE),
        deps(fake, http, phone_store, op=op)), 5)
    assert [v for v, _, _ in fake.requests if v == "hangup"] == []


async def test_a_failing_finish_does_not_escape(fake, http,
                                               phone_store) -> None:
    class Failing(Operator):
        async def finish(self, call_id: str) -> None:
            self.finished.append(call_id)
            raise RuntimeError("hand-off broke")

    op = Failing()
    sid = "live_finish"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE), deps(fake, http, phone_store, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.close(sid, "remote_hangup")
    assert await asyncio.wait_for(task, 5) is None
    [row] = calls(phone_store)
    assert row["outcome"] == "caller_hung_up"
    assert op.finished == [row["id"]]


async def test_a_call_hung_up_by_the_stop_is_interrupted(fake, http,
                                                        phone_store) -> None:
    op = Operator()
    sid = "live_stop"
    stop = []
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, op=op,
             cut_by_stop=lambda: bool(stop))))
    await wait_for(lambda: accepted(fake, sid))
    stop.append(True)
    await fake.close(sid, "close_requested")
    await asyncio.wait_for(task, 5)
    [row] = calls(phone_store)
    assert row["outcome"] == "interrupted"
    assert "cut off" in controller.headline(row)


async def test_no_accept_once_casa_is_stopping(fake, http, phone_store) -> None:
    op = Operator()
    accepts: list = []
    await asyncio.wait_for(controller.handle_call(
        Incoming("live_late", PRIORITY, LINE),
        deps(fake, http, phone_store, op=op, stopping=lambda: True,
             on_accept=lambda: accepts.append(1))), 5)
    assert fake.requests == [] and accepts == []
    [row] = calls(phone_store)
    assert row["outcome"] == "interrupted"
    assert op.finished == [row["id"]]


async def test_the_row_is_announced_before_it_is_written(
        fake, http, phone_store) -> None:
    seen: list = []

    def on_row(call_id: str) -> None:
        with phone_store.transaction() as conn:
            seen.append((call_id, pstore.get_call(conn, call_id)))

    op = Operator()
    sid = "live_order"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, op=op, on_row=on_row)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    [row] = calls(phone_store)
    assert seen == [(row["id"], None)]


async def test_a_row_that_was_not_written_is_forgotten(
        fake, http, phone_store) -> None:
    sid = "live_twice"
    with phone_store.transaction(write=True) as conn:
        # The session id is taken, so this call's row cannot be written.
        pstore.insert_call(conn, {"id": "other", "session_id": sid})
    announced: list = []
    forgotten: list = []
    op = Operator()
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, op=op, on_row=announced.append,
             forget_row=forgotten.append)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    assert len(announced) == 1 and forgotten == announced
    assert op.finished == [] and len(op.lines) == 1


async def test_a_row_is_forgotten_once_its_outcome_is_written(
        fake, http, phone_store) -> None:
    forgotten: list = []
    op = Operator()
    sid = "live_done"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, op=op, forget_row=forgotten.append)))
    await wait_for(lambda: accepted(fake, sid))
    assert forgotten == []
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    [row] = calls(phone_store)
    assert forgotten == [row["id"]]


async def test_a_row_whose_outcome_was_not_written_is_kept(
        fake, http, phone_store, monkeypatch) -> None:
    def broken(*args, **kwargs) -> None:
        raise RuntimeError("disk full")
    monkeypatch.setattr(pstore, "finish_call", broken)
    forgotten: list = []
    op = Operator()
    sid = "live_kept"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, op=op, forget_row=forgotten.append)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    [row] = calls(phone_store)
    assert row["outcome"] is None
    assert forgotten == []
    # The operator still learns who called, from which number and when.
    [line] = op.lines
    assert line.startswith(
        f"Unknown caller · {PRIORITY} · {controller.when_words(NOW)} · ")
    assert line.endswith(controller.UNRECORDED_NOTE)
    assert op.finished == []


async def test_putting_through_names_an_unknown_callers_full_number(
        fake, http, phone_store) -> None:
    with phone_store.transaction(write=True) as conn:
        pstore.set_grant(conn, "unknown", "transfer", "open")
    op = Operator()
    sid = "live_put"
    caller = "+39201110099"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, caller, LINE), deps(fake, http, phone_store, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.emit(sid, {"type": "session.delegation.created",
                          "delegation": {"id": "d1", "type": "delegation"}})
    await fake.emit(sid, {**tool_call_event("transfer", {"why": "asked"}),
                          "delegation_id": "d1"})
    await wait_for(lambda: any(v == "refer" for v, _, _ in fake.requests))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    assert op.lines == [f"Putting through: a caller on {caller}"]


async def test_no_log_line_of_a_call_carries_the_full_number(
        fake, http, phone_store, contacts, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="phone")
    caplog.set_level(logging.DEBUG, logger="live_voice")
    op = Operator()
    sid = "live_logged"
    task = asyncio.create_task(controller.handle_call(
        Incoming(sid, PRIORITY, LINE),
        deps(fake, http, phone_store, contacts=contacts, op=op)))
    await wait_for(lambda: accepted(fake, sid))
    await fake.emit(sid, {"type": "session.output_transcript.delta",
                          "delta": "Hello Bea", "start_ms": 0, "end_ms": 400})
    await fake.emit(sid, {"type": "session.delegation.created",
                          "delegation": {"id": "d1", "type": "delegation"}})
    await fake.emit(sid, {**tool_call_event("transfer", {"why": "asked"}),
                          "delegation_id": "d1"})
    await wait_for(lambda: any(v == "refer" for v, _, _ in fake.requests))
    await fake.close(sid, "remote_hangup")
    await asyncio.wait_for(task, 5)
    # The operator was given the number; the log never is.
    assert any(PRIORITY in line for line in op.lines)
    logged = [r for r in caplog.records
              if r.name.split(".")[0] in ("phone", "live_voice")]
    assert logged
    text = caplog.text
    for form in (PRIORITY, PRIORITY[1:], PRIORITY[3:]):
        assert form not in text
    assert PRIORITY[-4:] in text
