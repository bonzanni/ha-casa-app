"""What a call can cause: end it, put it through, alert the operator.

The runners talk to the local fake of the service through the real client;
the session is built but not run, and its action state is driven directly
(``actions.request``, ``actions.new_round`` for the caller speaking again).
Numbers and ids are invented."""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from live_voice.actions import Outcome
from live_voice.api import LiveApi
from live_voice.session import LiveSession, Profile
from phone import abilities
from phone_fakes import FakeOpenAI

KEY = "sk-test-not-a-real-key"
SID = "live_fake_1"
NUMBER = "+31612345678"
LINES = {"silence": "s", "too_long": "t", "transfer_failed": "f"}
REFUSED = Outcome("refused",
                  "The transfer did not go through. Take a message instead.")


class Notes:
    """The operator's side: every text sent, and what the send answers."""

    def __init__(self) -> None:
        self.sent: list = []
        self.answer = True
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.order: list[str] = []

    async def __call__(self, text) -> bool:
        self.sent.append(text)
        self.order.append("notify")
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
async def fake():
    async with FakeOpenAI() as server:
        yield server


@pytest.fixture
async def http():
    async with aiohttp.ClientSession() as session:
        yield session


def setup(fake, http, *, transfer_level="urgent", returned=False,
          notify=None, timeout_s=2.0, caller_label="Ann Example"):
    api = LiveApi(key=KEY, http=http, base_url=fake.base_url,
                  timeout_s=timeout_s)
    holder: list[LiveSession] = []
    built = abilities.build(
        api=api, sid=SID, session_ref=lambda: holder[0],
        transfer_number=NUMBER, transfer_level=transfer_level,
        returned=returned, notify=notify, caller_label=caller_label)
    profile = Profile(instructions="i", voice="marin", voice_model="v",
                      helper_model="h", helper_instructions="h",
                      decision_model="d", decision_instructions="d",
                      abilities=built)
    holder.append(LiveSession(api, SID, profile, lines=LINES))
    return holder[0], {a.name: a for a in built}


def refers(fake) -> list[dict]:
    return [body for verb, _, body in fake.requests if verb == "refer"]


def hangups(fake) -> list[dict]:
    return [body for verb, _, body in fake.requests if verb == "hangup"]


async def request(session, name, args=None):
    actions = session.actions
    return await actions.request(name, args or {"why": "asked"},
                                 round=actions.round, trigger="decision")


async def test_shapes(fake, http):
    _, found = setup(fake, http, returned=True, transfer_level="open")
    assert found["end_call"].kind == "final"
    assert found["alert_operator"].kind == "plain"
    assert found["end_call"].parameters == {
        "type": "object", "additionalProperties": False, "required": ["why"],
        "properties": {"why": {"type": "string",
                               "description": found["end_call"].parameters
                               ["properties"]["why"]["description"]}}}
    assert found["alert_operator"].parameters["required"] == ["reason"]


async def test_off_gives_no_transfer_and_no_alert(fake, http):
    _, found = setup(fake, http, transfer_level="off")
    assert set(found) == {"end_call"}


async def test_a_returned_call_can_alert_the_operator(fake, http):
    notes = Notes()
    session, found = setup(fake, http, transfer_level="off", returned=True,
                           notify=notes,
                           caller_label="the caller you were just put through to")
    assert set(found) == {"end_call", "alert_operator"}
    outcome = await session.actions.request(
        "alert_operator", {"reason": "a flood\nin the kitchen " + "x" * 400},
        round=1, trigger="helper")
    assert outcome == Outcome("done", "The alert was sent.")
    assert len(notes.sent) == 1
    text = notes.sent[0].text
    assert notes.sent[0].source == "casa"
    assert text == ("Urgent, from the phone: the caller you were just put "
                    "through to was put through, you did not pick up, and "
                    "they say it is an emergency. The details follow when "
                    "the call ends.")
    assert "flood" not in text and "kitchen" not in text and "x" * 5 not in text


async def test_an_alert_that_cannot_be_sent_is_refused(fake, http):
    notes = Notes()
    notes.answer = False
    session, found = setup(fake, http, transfer_level="off", returned=True,
                           notify=notes)
    outcome = await request(session, "alert_operator", {"reason": "fire"})
    assert outcome.status == "refused"


async def test_transfer_notifies_then_refers_then_waits(fake, http):
    notes = Notes()
    session, _ = setup(fake, http, notify=notes)
    outcome = await request(session, "transfer")
    assert outcome == Outcome("done", "")
    assert len(notes.sent) == 1
    assert notes.sent[0].text == "Putting through: Ann Example"
    assert notes.sent[0].source == "casa"
    assert refers(fake) == [{"target_uri": "tel:" + NUMBER}]
    verbs = [verb for verb, _, _ in fake.requests]
    assert verbs == ["refer"]
    assert session.actions.phase == "pending"


async def test_notify_comes_before_refer(fake, http):
    notes = Notes()
    notes.gate = asyncio.Event()
    session, _ = setup(fake, http, notify=notes)
    task = asyncio.create_task(request(session, "transfer"))
    await asyncio.wait_for(notes.entered.wait(), 2)
    await asyncio.sleep(0.05)
    assert refers(fake) == []
    notes.gate.set()
    assert (await task).status == "done"
    assert len(refers(fake)) == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("telegram down")])
async def test_a_failed_notification_does_not_stop_the_transfer(
        fake, http, failure):
    notes = Notes()
    notes.answer = failure
    session, _ = setup(fake, http, notify=notes)
    assert (await request(session, "transfer")).status == "done"
    assert len(refers(fake)) == 1


async def test_transfer_without_notify(fake, http):
    session, _ = setup(fake, http, notify=None)
    assert (await request(session, "transfer")).status == "done"
    assert len(refers(fake)) == 1


async def test_a_refused_transfer_may_be_tried_again_after_the_caller_speaks(
        fake, http):
    fake.status_for["refer"] = 403
    session, _ = setup(fake, http, notify=Notes())
    assert await request(session, "transfer") == REFUSED
    assert session.actions.phase == "active"
    # Same round, the other trigger: the stored outcome, nothing sent.
    again = await session.actions.request("transfer", {"why": "x"},
                                          round=session.actions.round,
                                          trigger="helper")
    assert again == REFUSED
    assert len(refers(fake)) == 1
    session.actions.new_round()
    del fake.status_for["refer"]
    assert (await request(session, "transfer")).status == "done"
    assert len(refers(fake)) == 2
    assert session.actions.phase == "pending"


@pytest.mark.parametrize("status", [500, 503])
async def test_a_server_error_counts_as_maybe_accepted(fake, http, status):
    fake.status_for["refer"] = status
    session, _ = setup(fake, http, notify=Notes())
    assert (await request(session, "transfer")).status == "done"
    assert session.actions.phase == "pending"
    session.actions.new_round()
    await request(session, "transfer")
    assert len(refers(fake)) == 1


async def test_an_unanswered_refer_is_never_followed_by_a_second(fake, http):
    fake.hang_for.add("refer")
    session, _ = setup(fake, http, notify=Notes(), timeout_s=0.2)
    assert (await request(session, "transfer")).status == "done"
    assert session.actions.phase == "pending"
    session.actions.new_round()
    second = await request(session, "transfer")
    assert second.status != "superseded"
    assert len(refers(fake)) == 1
    assert session.actions.phase == "pending"


async def test_a_session_that_ends_during_the_notification_is_not_put_through(
        fake, http):
    notes = Notes()
    notes.gate = asyncio.Event()
    session, _ = setup(fake, http, notify=notes)
    task = asyncio.create_task(request(session, "transfer"))
    await asyncio.wait_for(notes.entered.wait(), 2)
    session.actions.end()
    notes.gate.set()
    assert (await task) == Outcome("final", "ended")
    assert refers(fake) == []


async def test_a_caller_who_speaks_during_the_notification_supersedes_it(
        fake, http):
    notes = Notes()
    notes.gate = asyncio.Event()
    session, _ = setup(fake, http, notify=notes)
    task = asyncio.create_task(request(session, "transfer"))
    await asyncio.wait_for(notes.entered.wait(), 2)
    session.actions.new_round()
    notes.gate.set()
    assert (await task) == Outcome("superseded", "")
    assert refers(fake) == []
    # Not settled for the round: the caller asks again and it goes through.
    assert (await request(session, "transfer")).status == "done"
    assert len(refers(fake)) == 1


async def test_a_stale_request_does_not_notify(fake, http):
    notes = Notes()
    session, found = setup(fake, http, notify=notes)
    session.actions.new_round()
    assert await found["transfer"].run({"why": "x"}, 1) == Outcome("superseded", "")
    session.actions.end()
    assert (await found["transfer"].run({"why": "x"}, session.actions.round)
            == Outcome("final", "ended"))
    assert notes.sent == []
    assert refers(fake) == []


async def test_end_call_hangs_up_once(fake, http):
    _, found = setup(fake, http)
    assert await found["end_call"].run({"why": "bye"}, 1) == Outcome("done", "")
    assert len(hangups(fake)) == 1


async def test_a_failed_hang_up_is_refused(fake, http):
    fake.status_for["hangup"] = 404
    _, found = setup(fake, http)
    outcome = await found["end_call"].run({"why": "bye"}, 1)
    assert outcome.status == "refused"
