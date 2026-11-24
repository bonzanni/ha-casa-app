"""The live session, against the local fake of the service.

No network: the real client follows ``FakeOpenAI`` on localhost. Every clock
under test is a fraction of a second; the clocks a test does not exercise are
set far away (``FAR``). ``caller``/``agent`` emit transcript deltas the way
the service does; the two abilities are small functions that count their
calls. The key and session id are invented."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import aiohttp
import pytest

from live_voice.actions import Outcome
from live_voice.api import LiveApi
from live_voice.session import Ability, Line, LiveSession, Profile, Timing
from phone_fakes import FakeOpenAI, function_call, tool_call_event

KEY = "sk-test-not-a-real-key"
SID = "live_fake_1"
FAR = 30.0
TIMING = Timing(first_speech_s=FAR, quiet_s=0.05, silence_s=FAR, max_call_s=FAR,
                reconnect_s=0.3, decision_timeout_s=1.0, undecided_silence_s=0.15,
                goodbye_grace_s=0.2, speak_margin_s=0.02, pending_s=0.2)
SPOKEN = {"silence": "Are you still there?",
          "too_long": "I have to end this now.",
          "transfer_failed": "Nobody picked up. Take a message instead."}
SAY = "Immediately say the following exactly, then continue: "
PARAMETERS = {"properties": {"why": {"type": "string"}}, "required": ["why"]}
END = [function_call("end_call", {"why": "goodbye said"})]
TRANSFER = [function_call("transfer", {"why": "asked for"})]


async def until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the expected state never came"
        await asyncio.sleep(0.005)


class Call:
    """One session running against the fake."""

    def __init__(self, fake: FakeOpenAI, api: LiveApi, timing: Timing) -> None:
        self.fake = fake
        self.api = api
        #: What the ``transfer`` runner answers; "pending" marks the work
        #: pending and answers done, the way an accepted hand-over does; an
        #: exception is raised.
        self.transfer_answer: Outcome | Exception | str = Outcome("done", "")
        self.transfer_gate: asyncio.Event | None = None
        self.transfers: list[tuple[dict, int]] = []
        self.hangups_at: list[float] = []
        self.profile = Profile(
            instructions="voice rules", voice="marin", voice_model="voice-m",
            helper_model="helper-m", helper_instructions="helper rules",
            decision_model="decision-m", decision_instructions="decision rules",
            abilities=(
                Ability("transfer", "Hand the session over.", PARAMETERS,
                        self._transfer, "plain"),
                Ability("end_call", "End the session.", PARAMETERS,
                        self._end_call, "final"),
            ))
        self.session = LiveSession(api, SID, self.profile, timing=timing,
                                   lines=SPOKEN)
        self.started = time.monotonic()
        self.task = asyncio.create_task(self.session.run())

    async def _transfer(self, args: dict, round: int) -> Outcome:
        self.transfers.append((args, round))
        if self.transfer_gate is not None:
            await self.transfer_gate.wait()
        if isinstance(self.transfer_answer, Exception):
            raise self.transfer_answer
        if self.transfer_answer == "pending":
            self.session.actions.mark_pending("transfer")
            return Outcome("done", "")
        return self.transfer_answer

    async def _end_call(self, args: dict, round: int) -> Outcome:
        self.hangups_at.append(time.monotonic())
        await self.api.hangup(SID)
        return Outcome("done", "")

    # What the service sends.

    async def emit(self, event: dict) -> None:
        await self.fake.emit(SID, event)

    async def caller(self, text: str, start_ms: int = 0, end_ms: int = 0) -> None:
        await self.emit({"type": "session.input_transcript.delta", "delta": text,
                         "start_ms": start_ms, "end_ms": end_ms})

    async def agent(self, text: str, start_ms: int = 0, end_ms: int = 0) -> None:
        await self.emit({"type": "session.output_transcript.delta", "delta": text,
                         "start_ms": start_ms, "end_ms": end_ms})

    async def delegation(self, delegation_id: str) -> None:
        await self.emit({"type": "session.delegation.created",
                         "delegation": {"id": delegation_id, "type": "delegation"}})

    async def helper_call(self, delegation_id: str, name: str,
                          call_id: str = "call_1") -> None:
        await self.emit({**tool_call_event(name, {"why": "helper"}, call_id),
                         "delegation_id": delegation_id})

    # What the session did.

    def verbs(self, verb: str) -> list[dict]:
        return [body for sent, _, body in self.fake.requests if sent == verb]

    @property
    def decisions(self) -> list[dict]:
        return self.verbs("responses")

    @property
    def hangups(self) -> list[dict]:
        return self.verbs("hangup")

    @property
    def appended(self) -> list[str]:
        return [e["content"] for e in self.fake.ws_sent
                if e["type"] == "session.instructions.append"]

    @property
    def log(self) -> list[tuple[str, str, int, str]]:
        return [(e["ability"], e["trigger"], e["round"], e["status"])
                for e in self.session.actions.log]

    async def output(self, call_id: str = "call_1") -> dict:
        """The ``function_call_output`` the session sent for *call_id*."""
        await self.fake.wait_sent("response.create")
        for event in self.fake.ws_sent:
            if event.get("event_id") == f"r_{call_id}":
                assert event["type"] == "response.item.create"
                assert event["item"]["type"] == "function_call_output"
                assert event["item"]["call_id"] == call_id
                return json.loads(event["item"]["output"])
        raise AssertionError(f"no output was sent for {call_id}")

    async def result(self, close: str | None = None):
        if close is not None:
            await self.fake.close(SID, close)
        return await asyncio.wait_for(self.task, 3)


@pytest.fixture
async def fake():
    async with FakeOpenAI() as server:
        yield server


@pytest.fixture
async def api(fake):
    async with aiohttp.ClientSession() as http:
        yield LiveApi(key=KEY, http=http, timeout_s=2.0, base_url=fake.base_url)


@pytest.fixture
async def start(fake, api):
    calls: list[Call] = []

    def _start(**timing) -> Call:
        call = Call(fake, api, replace(TIMING, **timing))
        calls.append(call)
        return call

    yield _start
    for call in calls:
        call.task.cancel()
    await asyncio.gather(*(call.task for call in calls), return_exceptions=True)


# The decision trigger.

async def test_a_quiet_agent_line_is_decided_once_and_can_end_the_session(
        fake, start) -> None:
    fake.responses_queue = [END]
    call = start()
    await call.emit({"type": "session.started"})
    await call.agent(" Goodbye")
    await call.agent(" then.", 200, 400)
    result = await call.result()
    assert len(call.decisions) == 1
    assert call.decisions[0]["input"] == "The call so far:\nAssistant: Goodbye then."
    assert call.decisions[0]["model"] == "decision-m"
    assert len(call.hangups) == 1
    assert result.phase == "ended"
    assert result.closed_reason == "close_requested"
    assert result.lines == (Line("agent", "Goodbye then.", 0, 400),)
    assert [(a["ability"], a["trigger"], a["round"], a["status"])
            for a in result.actions] == [("end_call", "decision", 1, "done")]
    assert result.started <= result.ended


async def test_two_close_agent_deltas_are_one_line_and_one_decision(
        fake, start) -> None:
    call = start()
    await call.agent(" One")
    await asyncio.sleep(0.02)
    await call.agent(" moment.")
    await until(lambda: len(call.decisions) == 1)
    await asyncio.sleep(0.15)
    result = await call.result(close="remote_hangup")
    assert len(call.decisions) == 1
    assert [line.text for line in result.lines] == ["One moment."]


async def test_a_decision_the_caller_spoke_over_does_not_act(fake, start) -> None:
    gate = asyncio.Event()
    fake.responses_queue = [("wait", gate, END)]
    call = start()
    await call.agent(" Goodbye.")
    await until(lambda: len(call.decisions) == 1)
    await call.caller(" Wait, one more thing.")
    await until(lambda: call.session.actions.round == 2)
    gate.set()
    await until(lambda: call.log == [("end_call", "decision", 1, "superseded")])
    await asyncio.sleep(0.1)
    assert call.hangups == []
    result = await call.result(close="remote_hangup")
    assert result.closed_reason == "remote_hangup"
    assert [line.who for line in result.lines] == ["agent", "caller"]


async def test_a_caller_delta_before_the_quiet_moment_cancels_the_decision(
        fake, start) -> None:
    fake.responses_queue = [END]
    call = start(quiet_s=0.2)
    await call.agent(" Is that all?")
    await call.caller(" No,")
    await call.caller(" there is")
    await call.caller(" more.")
    await asyncio.sleep(0.4)
    assert call.decisions == []
    assert call.hangups == [] and call.verbs("refer") == []
    result = await call.result(close="remote_hangup")
    assert [(line.who, line.text) for line in result.lines] == [
        ("agent", "Is that all?"), ("caller", "No, there is more.")]
    assert result.actions == ()


async def test_a_refused_decision_is_told_once_and_ending_still_works(
        fake, start) -> None:
    fake.responses_queue = [TRANSFER, TRANSFER, END]
    call = start()
    call.transfer_answer = Outcome("refused", "It did not go through.")
    await call.agent(" I will put you through.")
    await fake.wait_sent("session.instructions.append")
    await call.agent(" One moment.")
    await until(lambda: len(call.decisions) == 2)
    await until(lambda: len(call.log) == 2)
    await asyncio.sleep(0.1)
    assert len(call.transfers) == 1
    assert call.appended == ["It did not go through."]
    await call.agent(" Goodbye.")
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.phase == "ended"
    assert [(a["ability"], a["status"]) for a in result.actions] == [
        ("transfer", "refused"), ("transfer", "refused"), ("end_call", "done")]


async def test_a_refusal_in_the_action_states_own_words_is_not_told(
        fake, start) -> None:
    fake.responses_queue = [TRANSFER]
    call = start()
    call.transfer_answer = RuntimeError("boom")
    await call.agent(" I will put you through.")
    await until(lambda: call.log == [("transfer", "decision", 1, "refused")])
    await asyncio.sleep(0.1)
    assert len(call.transfers) == 1
    assert call.appended == []
    await call.result(close="remote_hangup")


# The helper trigger.

async def test_both_triggers_asking_in_one_round_run_the_ability_once(
        fake, start) -> None:
    fake.responses_queue = [TRANSFER]
    call = start()
    call.transfer_gate = asyncio.Event()
    await call.delegation("d1")
    await call.agent(" I will put you through.")
    await call.helper_call("d1", "transfer")
    await until(lambda: len(call.decisions) == 1)
    await until(lambda: len(call.transfers) == 1)
    await asyncio.sleep(0.05)
    call.transfer_gate.set()
    output = await call.output()
    await until(lambda: len(call.log) == 2)
    assert len(call.transfers) == 1
    assert sorted(call.log) == [("transfer", "decision", 1, "done"),
                                ("transfer", "helper", 1, "done")]
    assert output == {"status": "done", "detail": ""}
    created = await fake.wait_sent("response.create")
    assert created[0]["event_id"] == "c_call_1"
    await call.result(close="remote_hangup")


async def test_a_helper_call_from_before_the_caller_spoke_is_superseded(
        fake, start) -> None:
    call = start()
    await call.agent(" How can I help?")
    await call.delegation("d1")
    await call.caller(" Actually, never mind.")
    await call.helper_call("d1", "transfer")
    assert await call.output() == {"status": "superseded", "detail": ""}
    assert call.transfers == []
    assert ("transfer", "helper", 1, "superseded") in call.log
    await call.result(close="remote_hangup")


async def test_a_helper_call_of_an_unseen_delegation_never_runs(fake, start) -> None:
    call = start()
    await call.agent(" How can I help?")
    await call.helper_call("d_unseen", "transfer", "call_7")
    assert await call.output("call_7") == {"status": "superseded", "detail": ""}
    assert call.transfers == []
    await call.result(close="remote_hangup")


# A decision that could not be made.

async def test_a_decision_failing_twice_then_caller_speech_goes_on(
        fake, start) -> None:
    fake.responses_queue = [RuntimeError("down"), RuntimeError("down")]
    call = start(undecided_silence_s=0.3)
    await call.agent(" Goodbye.")
    await until(lambda: len(call.decisions) == 2)
    await call.caller(" Hello?")
    await asyncio.sleep(0.5)
    assert call.hangups == []
    result = await call.result(close="remote_hangup")
    assert result.actions == ()
    assert result.closed_reason == "remote_hangup"


async def test_a_decision_failing_twice_then_silence_ends_the_session(
        fake, start) -> None:
    fake.responses_queue = [RuntimeError("down"), RuntimeError("down")]
    call = start()
    await call.agent(" Goodbye.")
    result = await call.result()
    assert len(call.decisions) == 2
    assert len(call.hangups) == 1
    assert result.closed_reason == "clock:undecided"
    assert result.phase == "ended"
    assert result.actions == ()


async def test_a_decision_timing_out_twice_then_silence_ends_the_session(
        fake, start) -> None:
    fake.hang_for.add("responses")
    call = start(decision_timeout_s=0.1)
    await call.agent(" Goodbye.")
    result = await call.result()
    assert len(call.decisions) == 2
    assert len(call.hangups) == 1
    assert result.closed_reason == "clock:undecided"


async def test_no_action_and_silence_waits_for_the_silence_clock(fake, start) -> None:
    call = start(silence_s=1.0)
    await call.agent(" Take your time.")
    await until(lambda: len(call.decisions) == 1)
    await asyncio.sleep(0.4)
    assert call.hangups == []
    assert call.appended == []
    await call.result(close="remote_hangup")


# The clock.

async def test_no_agent_speech_ends_the_session(fake, start) -> None:
    call = start(first_speech_s=0.15)
    await call.caller(" Hello?")
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.closed_reason == "clock:first_speech"
    assert result.phase == "ended"
    assert result.ended - result.started >= 0.15


async def test_silence_says_the_line_and_then_ends_the_session(fake, start) -> None:
    call = start(silence_s=0.15, goodbye_grace_s=0.15)
    await call.agent(" Hello.")
    await fake.wait_sent("session.instructions.append")
    assert call.appended == [SAY + SPOKEN["silence"]]
    assert call.hangups == []
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.closed_reason == "clock:silence"
    assert fake.ws_sent[-1]["delegation_id"] is None
    assert isinstance(fake.ws_sent[-1]["event_id"], str)


async def test_caller_speech_after_the_silence_line_keeps_the_session(
        fake, start) -> None:
    call = start(silence_s=0.3, goodbye_grace_s=0.1)
    await call.agent(" Hello.")
    await fake.wait_sent("session.instructions.append")
    await call.caller(" Yes, I am here.")
    await asyncio.sleep(0.2)
    assert call.hangups == []
    result = await call.result(close="remote_hangup")
    assert call.hangups == []
    assert result.closed_reason == "remote_hangup"


async def test_caller_speech_after_the_too_long_line_changes_nothing(
        fake, start) -> None:
    call = start(max_call_s=0.2, goodbye_grace_s=0.1)
    await call.agent(" Hello.")
    await fake.wait_sent("session.instructions.append")
    assert call.appended == [SAY + SPOKEN["too_long"]]
    await call.caller(" But I was not done.")
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.closed_reason == "clock:max"
    assert len(call.appended) == 1
    assert result.ended - result.started < 0.2 + 0.1 + 0.15


async def test_a_failed_hangup_still_ends_the_session(fake, start) -> None:
    fake.status_for["hangup"] = 500
    call = start(first_speech_s=0.1)
    result = await call.result()
    assert len(call.hangups) == 1
    assert (result.closed_reason, result.phase) == ("clock:first_speech", "ended")


# The control channel.

async def test_a_dropped_socket_that_cannot_be_reattached_is_lost(
        fake, start) -> None:
    call = start(reconnect_s=0.25)
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    fake.attach_refuse = True
    dropped = time.monotonic()
    await fake.drop_ws(SID)
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.closed_reason == "lost"
    assert result.phase == "ended"
    assert result.ended - dropped >= 0.25
    assert fake.attach_count >= 3  # the first attach and at least two retries


async def test_a_first_attach_that_never_completes_is_lost(fake, start) -> None:
    fake.attach_refuse = "hang"
    call = start(reconnect_s=0.25)
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.closed_reason == "lost"
    assert 0.25 <= result.ended - result.started < 1.5
    assert result.lines == ()


async def test_a_dropped_socket_is_reattached_and_followed(fake, start) -> None:
    fake.responses_queue = [[], END]
    call = start()
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    await fake.drop_ws(SID)
    await until(lambda: fake.attach_count == 2)
    await call.caller(" Thanks, bye.")
    await call.agent(" Goodbye.")
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.closed_reason == "close_requested"
    assert [line.who for line in result.lines] == ["agent", "caller", "agent"]
    assert result.actions[-1]["round"] == 2


async def test_events_the_session_has_no_use_for_do_not_stop_it(fake, start) -> None:
    fake.responses_queue = [END]
    call = start()
    await call.emit({"type": "error", "error": {"code": "something_odd"}})
    await call.emit({"type": "session.output_transcript.delta", "delta": None})
    await call.emit({"type": "session.delegation.created", "delegation": "d1"})
    await call.emit({"type": "response.event", "delegation_id": ["d1"],
                     "event": {"type": "response.output_item.done", "item": 7}})
    await call.emit({"type": "session.usage.updated"})
    await call.agent(" Goodbye.")
    result = await call.result()
    assert len(call.hangups) == 1
    assert [line.text for line in result.lines] == ["Goodbye."]


async def test_frames_that_are_not_events_are_skipped(fake, start) -> None:
    call = start()
    ws = await fake._socket(SID)  # the fake has no control for raw frames
    await ws.send_str("{not json")
    await ws.send_bytes(b"\x00\x01")
    await ws.send_str("[1, 2]")
    await ws.send_str('"just text"')
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    await call.emit({"type": "session.closed"})  # no reason given
    result = await call.result()
    assert (result.closed_reason, result.phase) == ("closed", "ended")
    assert [line.text for line in result.lines] == ["Hello."]
    assert call.hangups == []
    assert _session_tasks() == []


async def test_a_session_the_other_side_closed_is_not_hung_up(fake, start) -> None:
    call = start()
    await call.agent(" Hello.")
    result = await call.result(close="remote_hangup")
    assert result.closed_reason == "remote_hangup"
    assert result.phase == "ended"
    assert call.hangups == []


def _session_tasks() -> list[str]:
    return [task.get_name() for task in asyncio.all_tasks()
            if task.get_name().startswith("live-voice")]


async def test_run_leaves_no_task_behind(fake, start) -> None:
    gate = asyncio.Event()
    fake.responses_queue = [("wait", gate, END)]
    call = start()
    call.transfer_gate = asyncio.Event()
    await call.delegation("d1")
    await call.agent(" Hello.")
    await call.helper_call("d1", "transfer")
    await until(lambda: len(call.decisions) == 1 and len(call.transfers) == 1)
    await call.agent(" Still there?")  # a quiet timer is waiting as well
    # The reader, the clock, a waiting decision, a helper call in its runner
    # and a quiet timer.
    await until(lambda: len(_session_tasks()) == 5)
    result = await call.result(close="remote_hangup")
    assert _session_tasks() == []
    assert result.actions[-1]["status"] == "refused"  # the interrupted runner


# Work that was accepted but has not finished.

async def test_pending_work_and_a_closed_session_is_a_hand_over(fake, start) -> None:
    call = start()
    call.transfer_answer = "pending"
    await call.delegation("d1")
    await call.agent(" I will put you through.")
    await call.helper_call("d1", "transfer")
    assert await call.output() == {"status": "done", "detail": ""}
    assert call.session.actions.phase == "pending"
    result = await call.result(close="close_requested")
    assert result.phase == "handed_off"
    assert result.closed_reason == "close_requested"
    assert call.hangups == []


async def test_pending_work_that_never_finishes_is_told_as_failed(
        fake, start) -> None:
    call = start()
    call.transfer_answer = "pending"
    await call.delegation("d1")
    await call.agent(" I will put you through.")
    await call.helper_call("d1", "transfer")
    await until(lambda: call.session.actions.phase == "pending")
    await fake.wait_sent("session.instructions.append")
    assert call.session.actions.phase == "active"
    assert call.appended == [SPOKEN["transfer_failed"]]
    assert call.hangups == []
    result = await call.result(close="remote_hangup")
    assert result.phase == "ended"


async def test_a_failed_decision_does_not_end_a_session_with_pending_work(
        fake, start) -> None:
    fake.responses_queue = [RuntimeError("down"), RuntimeError("down")]
    call = start(pending_s=0.6, undecided_silence_s=0.1)
    call.transfer_answer = "pending"
    await call.delegation("d1")
    await call.agent(" I will put you through.")
    await call.helper_call("d1", "transfer")
    await until(lambda: len(call.decisions) == 2)
    await asyncio.sleep(0.3)
    assert call.session.actions.phase == "pending"
    assert call.hangups == []
    result = await call.result(close="close_requested")
    assert result.phase == "handed_off"


async def test_pending_work_takes_over_from_a_decision_that_failed(
        fake, start) -> None:
    fake.responses_queue = [RuntimeError("down"), RuntimeError("down")]
    call = start(pending_s=0.8, undecided_silence_s=0.2)
    call.transfer_answer = "pending"
    await call.delegation("d1")
    await call.agent(" I will put you through.")
    await until(lambda: len(call.decisions) == 2)
    await call.helper_call("d1", "transfer")
    await until(lambda: call.session.actions.phase == "pending")
    await asyncio.sleep(0.4)
    assert call.hangups == []
    result = await call.result(close="close_requested")
    assert result.phase == "handed_off"


# The last line is played before the session is ended.

async def test_ending_waits_until_the_last_agent_line_was_played(fake, start) -> None:
    fake.responses_queue = [END]
    call = start(goodbye_grace_s=2.0)
    seen = time.monotonic()
    await call.emit({"type": "session.started"})
    await call.agent(" Goodbye.", 0, 400)
    result = await call.result()
    assert len(call.hangups) == 1
    assert call.hangups_at[0] - seen >= 0.4 + TIMING.speak_margin_s
    assert result.phase == "ended"


async def test_ending_never_waits_longer_than_the_grace(fake, start) -> None:
    fake.responses_queue = [END]
    call = start(goodbye_grace_s=0.1)
    await call.emit({"type": "session.started"})
    await call.agent(" Goodbye.", 0, 20_000)
    result = await call.result()
    assert len(call.hangups) == 1
    assert result.ended - result.started < 1.5


async def test_caller_speech_while_the_last_line_plays_keeps_the_session(
        fake, start) -> None:
    fake.responses_queue = [END, END]
    call = start(goodbye_grace_s=2.0)
    await call.emit({"type": "session.started"})
    await call.agent(" Goodbye.", 0, 600)
    await until(lambda: len(call.decisions) == 1)
    await asyncio.sleep(0.05)  # the request is now waiting for the line to end
    await call.caller(" Oh, one more thing.")
    await until(lambda: call.log == [("end_call", "decision", 1, "superseded")])
    await asyncio.sleep(0.7)  # past the moment the first line finished playing
    assert call.hangups == []
    await call.agent(" Sure. Goodbye.", 0, 0)
    result = await call.result()
    assert len(call.hangups) == 1
    assert call.log[-1] == ("end_call", "decision", 2, "done")
    assert result.phase == "ended"


# A session is never ended on the other side's unanswered words.

async def test_a_helper_cannot_end_the_session_while_the_caller_speaks(
        fake, start) -> None:
    call = start(goodbye_grace_s=1.0)
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    await call.caller(" I wanted")
    await call.delegation("d1")
    await call.helper_call("d1", "end_call")
    await asyncio.sleep(0.05)  # the request is now waiting for the agent
    spoke = time.monotonic()
    await call.caller(" to ask something.")
    assert await call.output() == {"status": "superseded", "detail": ""}
    assert time.monotonic() - spoke < 0.5  # at once, not after the grace
    assert call.hangups == []
    assert call.log == [("end_call", "helper", 2, "superseded")]
    result = await call.result(close="remote_hangup")
    assert result.closed_reason == "remote_hangup"


async def test_a_helper_cannot_end_the_session_on_unanswered_words(
        fake, start) -> None:
    call = start()
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    await call.caller(" Bye.")
    await call.delegation("d1")
    asked = time.monotonic()
    await call.helper_call("d1", "end_call")
    assert await call.output() == {"status": "superseded", "detail": ""}
    assert 0 <= time.monotonic() - asked - TIMING.goodbye_grace_s < 0.5
    assert call.hangups == []
    # Nothing was settled: once the agent has answered, the round can end.
    fake.responses_queue = [END]
    await call.agent(" Goodbye.")
    result = await call.result()
    assert len(call.hangups) == 1
    assert call.log == [("end_call", "helper", 2, "superseded"),
                        ("end_call", "decision", 2, "done")]
    assert result.phase == "ended"


async def test_an_agent_answer_does_not_revive_a_request_the_caller_spoke_over(
        fake, start) -> None:
    fake.responses_queue = [END]
    call = start(goodbye_grace_s=2.0)
    await call.emit({"type": "session.started"})
    await call.agent(" Goodbye.", 0, 600)
    await until(lambda: len(call.decisions) == 1)
    await asyncio.sleep(0.05)  # the request is now waiting for the line to end
    # One network read can deliver both deltas before the waiting request
    # looks again; the fake cannot batch frames, so the session is handed
    # the two events back to back.
    for kind, text in (("input", " Wait."), ("output", " Yes?")):
        call.session._on_event({"type": f"session.{kind}_transcript.delta",
                                "delta": text, "start_ms": 0, "end_ms": 0})
    await until(lambda: call.log == [("end_call", "decision", 1, "superseded")])
    await asyncio.sleep(0.2)
    assert call.hangups == []
    await call.result(close="remote_hangup")


async def test_a_helper_cannot_end_a_session_in_which_nobody_spoke(
        fake, start) -> None:
    call = start(goodbye_grace_s=0.1)
    await call.delegation("d1")
    await call.helper_call("d1", "end_call")
    assert await call.output() == {"status": "superseded", "detail": ""}
    assert call.hangups == []
    await call.result(close="remote_hangup")


async def test_a_helper_ends_the_session_once_the_agents_answer_was_played(
        fake, start) -> None:
    call = start(goodbye_grace_s=1.0)
    seen = time.monotonic()
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    await call.caller(" Bye.")
    await call.delegation("d1")
    await call.helper_call("d1", "end_call")
    await asyncio.sleep(0.1)
    assert call.hangups == []
    await call.agent(" Goodbye.", 0, 600)
    result = await call.result()
    assert len(call.hangups) == 1
    assert call.hangups_at[0] - seen >= 0.6 + TIMING.speak_margin_s
    assert call.log[0] == ("end_call", "helper", 2, "done")
    assert result.phase == "ended"


# What the transport gets.

def test_the_session_body_names_the_abilities_as_function_tools() -> None:
    async def run(args: dict, round: int) -> Outcome:
        return Outcome("done", "")

    profile = Profile(
        instructions="voice rules", voice={"id": "voice_x"}, voice_model="voice-m",
        helper_model="helper-m", helper_instructions="helper rules",
        decision_model="decision-m", decision_instructions="decision rules",
        abilities=(Ability("end_call", "End the session.", PARAMETERS, run, "final"),))
    assert profile.session_body() == {
        "type": "live", "model": "voice-m", "instructions": "voice rules",
        "audio": {"output": {"voice": {"id": "voice_x"}}},
        "delegation": {"type": "responses", "responses": {
            "model": "helper-m", "instructions": "helper rules",
            "tools": [{"type": "function", "name": "end_call",
                       "description": "End the session.",
                       "parameters": {"type": "object",
                                      "properties": {"why": {"type": "string"}},
                                      "required": ["why"],
                                      "additionalProperties": False}}],
            "tool_choice": "auto"}}}


async def test_say_and_tell_append_instructions(fake, start) -> None:
    call = start()
    await call.agent(" Hello.")
    await until(lambda: len(call.decisions) == 1)
    await call.session.say("One moment please.")
    await call.session.tell("The other side is busy.")
    await fake.wait_sent("session.instructions.append", 2)
    assert call.appended == [SAY + "One moment please.", "The other side is busy."]
    ids = [e["event_id"] for e in fake.ws_sent]
    assert len(set(ids)) == 2
    await call.result(close="remote_hangup")


async def test_the_default_timing() -> None:
    assert Timing() == Timing(
        first_speech_s=8.0, quiet_s=1.0, silence_s=45.0, max_call_s=600.0,
        reconnect_s=10.0, decision_timeout_s=6.0, undecided_silence_s=8.0,
        goodbye_grace_s=8.0, speak_margin_s=0.6, pending_s=30.0)
