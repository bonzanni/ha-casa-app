"""One live voice session, followed over its control channel.

:class:`LiveSession` attaches to a session that was already accepted and
follows its events until it closes or its clock ends it. It knows nothing
about what carries the audio; the transport supplies the :class:`Profile`
(prompts, voice, abilities) and reads the :class:`Result`.

Who is speaking is inferred from the transcript, because the service has no
event for the end of a turn. Consecutive transcript deltas from one side are
one :class:`Line`. When the other side starts to speak after an agent line a
new *round* begins (:meth:`ActionState.new_round`), and every request for an
ability names the round it was decided in, so a decision made for a turn that
is already over never acts.

Two triggers ask for abilities, and both go through ``actions``:

- the helper model of the session, whose function calls arrive as
  ``response.event``; a call belongs to the round in which its delegation was
  created, and a call of a delegation this session never saw created does not
  run at all;
- the decision step (:mod:`live_voice.decision`), asked once each time the
  agent has been quiet for ``quiet_s`` after saying something. Speech from the
  other side before that moment cancels it; one already asked is left to
  finish and is judged by its round.

An ability of kind ``final`` ends the session, and a session is never ended
on the other side's unanswered words. Before such an ability runs, the session
waits for the agent to have the last word and for that line to have been
played; if the other side says anything meanwhile, or the agent says nothing
after their words, the ability does not run.

The clock (:mod:`live_voice.clock`) ends the session without any model: when
the agent never speaks, when both sides stay silent, when a decision could
not be made and nobody speaks, when the session lasts too long, and when the
control channel is lost and cannot be attached again in time. A silence is
checked on before it ends anything: the agent is first told to ask whether
the other side is still there, and only when that brings no word from them
is the closing line said and the session ended.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Literal, Mapping

import aiohttp

from live_voice import decision
from live_voice.actions import ActionState, Outcome, Runner
from live_voice.api import ApiError, LiveApi, parse_arguments
from live_voice.clock import Clock

logger = logging.getLogger(__name__)

SAY_PREFIX = "Immediately say the following exactly, then continue: "
#: The reason of a session that one of its own final abilities ended.
CLOSE_REQUESTED = "close_requested"
#: The longest pause between two attach attempts.
RETRY_PAUSE_S = 0.5
#: How long closing the control channel may wait for the other end.
CLOSE_WAIT_S = 2.0

_NOT_ATTACHED = (aiohttp.ClientError, asyncio.TimeoutError, OSError)
_WS_GONE = (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR)


@dataclass(frozen=True)
class Ability:
    """Something a model may cause during a session.

    ``parameters`` is the JSON Schema of the arguments (``properties``,
    ``required``); ``run`` gets the arguments and the round of the request.
    A ``final`` ability ends the session when it is done."""
    name: str
    description: str
    parameters: dict
    run: Callable[[dict, int], Awaitable[Outcome]]
    kind: Literal["plain", "final"]


@dataclass(frozen=True)
class Profile:
    instructions: str
    voice: str | dict
    voice_model: str
    helper_model: str
    helper_instructions: str
    decision_model: str
    decision_instructions: str
    abilities: tuple[Ability, ...]

    def session_body(self) -> dict:
        """The ``session`` of the accept request."""
        return {
            "type": "live", "model": self.voice_model,
            "instructions": self.instructions,
            "audio": {"output": {"voice": self.voice}},
            "delegation": {"type": "responses", "responses": {
                "model": self.helper_model,
                "instructions": self.helper_instructions,
                "tools": decision.function_tools(self.abilities),
                "tool_choice": "auto"}},
        }


@dataclass(frozen=True)
class Timing:
    """Seconds. ``first_speech_s``: the agent must have spoken this long after
    the attach. ``quiet_s``: no further agent delta for this long means the
    agent has finished saying something. ``check_in_s``: nobody has been
    speaking for this long since someone was last heard, and the agent is
    told to check on the other side. ``silence_s``: nobody has been speaking
    for this long since that check-in, without a word from the other side,
    and the session is closed. ``undecided_silence_s``: nobody has been
    speaking for this long since a decision could not be made. These three
    are never counted from before the agent's latest line has been played.
    ``max_call_s``: the longest session. ``reconnect_s``: how long an attach
    may be retried. ``decision_timeout_s``: one decision call.
    ``goodbye_grace_s``: the longest wait for a line to be played.
    ``speak_margin_s``: added to a line's end before it counts as played.
    ``pending_s``: how long accepted work may stay unfinished."""
    first_speech_s: float = 8.0
    quiet_s: float = 1.0
    check_in_s: float = 7.0
    silence_s: float = 10.0
    max_call_s: float = 600.0
    reconnect_s: float = 10.0
    decision_timeout_s: float = 6.0
    undecided_silence_s: float = 8.0
    goodbye_grace_s: float = 8.0
    speak_margin_s: float = 0.6
    pending_s: float = 30.0


@dataclass(frozen=True)
class Line:
    """What one side said without the other speaking in between; the times
    are milliseconds from the start of the session."""
    who: Literal["caller", "agent"]
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Result:
    """How a session went. ``closed_reason`` is the service's reason,
    ``clock:<which>`` when the clock ended it, or ``lost``; ``started`` and
    ``ended`` are ``time.monotonic()`` values, like the ``t`` of an action.

    Two delays, for tuning only. ``first_speech_ms``: from the moment the
    session began to be followed (it has just been accepted) to the agent's
    first transcript delta; ``None`` when the agent never spoke.
    ``reply_gaps_ms``: for each line of the other side that the agent
    answered, the time from that line's end to the start of the answer."""
    lines: tuple[Line, ...]
    actions: tuple[dict, ...]
    phase: str
    closed_reason: str
    started: float
    ended: float
    first_speech_ms: int | None = None
    reply_gaps_ms: tuple[int, ...] = ()


class _Actions(ActionState):
    """The action state of one session; it reports the moment work becomes
    pending, which is when the clock starts to wait for it."""

    def __init__(self, runners: Mapping[str, Runner], *,
                 final_abilities: frozenset[str],
                 on_pending: Callable[[], None]) -> None:
        super().__init__(runners, final_abilities=final_abilities)
        self._on_pending = on_pending

    def mark_pending(self, ability: str) -> None:
        before = self.phase
        super().mark_pending(ability)
        if before != "pending" and self.phase == "pending":
            self._on_pending()


class LiveSession:
    """Follows one accepted session; ``lines`` holds the fixed lines in the
    session's language: ``silence`` and ``too_long`` are said as written,
    ``check_in`` and ``transfer_failed`` are told to the agent as context."""

    def __init__(self, api: LiveApi, sid: str, profile: Profile, *,
                 timing: Timing = Timing(), lines: Mapping[str, str]) -> None:
        self._api = api
        self._sid = sid
        self._profile = profile
        self._timing = timing
        self._spoken = dict(lines)
        self._clock = Clock(timing, self._on_clock)
        self._final = frozenset(a.name for a in profile.abilities
                                if a.kind == "final")
        self.actions: ActionState = _Actions(
            {a.name: self._noted(self._after_last_line(a.run)
                                 if a.kind == "final" else a.run)
             for a in profile.abilities},
            final_abilities=self._final, on_pending=self._clock.pending)
        self._lines: list[Line] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tasks: set[asyncio.Task] = set()
        self._done = asyncio.Event()
        self._reason: str | None = None
        #: When the first event was seen: the session's zero for ``*_ms``.
        self._zero: float | None = None
        #: When the agent's first transcript delta arrived.
        self._first_agent: float | None = None
        self._agent_end_ms = 0
        #: Count transcript deltas, to tell whether anyone spoke since.
        self._deltas = 0
        self._caller_deltas = 0
        self._quiet: asyncio.Task | None = None
        self._delegations: dict[str, int] = {}
        self._told: set[int] = set()
        #: The outcomes runners themselves returned, by ``id``.
        self._from_runner: dict[int, Outcome] = {}
        #: Waiters for the next delta, each with the side it waits for.
        self._speech_waiters: dict[asyncio.Future, str | None] = {}
        self._appends = 0

    # The run.

    async def run(self) -> Result:
        """Follow the session until it is over. Never raises (a cancellation
        passes through once every task of the session has stopped)."""
        started = time.monotonic()
        self._clock.start()
        self._spawn(self._vital(self._follow), "reader")
        self._spawn(self._vital(self._clock.run), "clock")
        try:
            await self._done.wait()
        finally:
            await self._stop()
        lines = tuple(self._lines)
        return Result(
            lines=lines,
            actions=tuple(dict(entry) for entry in self.actions.log),
            phase=self.actions.phase, closed_reason=self._reason or "lost",
            started=started, ended=time.monotonic(),
            first_speech_ms=None if self._first_agent is None
            else max(0, round((self._first_agent - started) * 1000)),
            reply_gaps_ms=_reply_gaps(lines))

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"live-voice {what}")
        self._tasks.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("%s failed", task.get_name(),
                           exc_info=task.exception())

    async def _stop(self) -> None:
        """Cancel every task of the session and wait until each has ended."""
        while self._tasks:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)

    async def _vital(self, work: Callable[[], Awaitable[None]]) -> None:
        """Run the reader or the clock; without either nobody would end the
        session, so one that fails ends it."""
        try:
            await work()
        except Exception:  # noqa: BLE001 — the session must still end
            logger.warning("live session stopped on an unexpected error",
                           exc_info=True)
            await self._end("lost")

    async def _end(self, reason: str) -> bool:
        """End the session from this side: no ability runs from here on, and
        the hang-up is asked for even if nothing answers. Always true."""
        if self._reason is not None:
            return True
        self._reason = reason
        self.actions.end()
        try:
            await self._api.hangup(self._sid)
        except ApiError as exc:
            logger.warning("live session hang-up failed: %s (status %s)",
                           exc.code, exc.status)
        except Exception:  # noqa: BLE001 — the session counts as ended
            logger.warning("live session hang-up failed", exc_info=True)
        finally:
            self._done.set()
        return True

    def _finish(self, reason: str) -> None:
        """The session is over without a hang-up of the clock's."""
        if self._reason is None:
            self._reason = reason
        self._done.set()

    # The control channel.

    async def _follow(self) -> None:
        """Attach and read events; attach again when the channel drops."""
        deadline = self._clock.started + self._timing.reconnect_s
        while True:
            ws = await self._attach(deadline)
            if ws is None:
                await self._end("lost")
                return
            self._ws = ws
            self._clock.attached()
            try:
                closed = await self._read(ws)
            finally:
                self._ws = None
                await self._close(ws)
            if closed:
                return
            logger.info("live session control channel dropped, attaching again")
            deadline = time.monotonic() + self._timing.reconnect_s
            await asyncio.sleep(self._retry_pause(deadline))

    def _retry_pause(self, deadline: float) -> float:
        pause = min(RETRY_PAUSE_S, self._timing.reconnect_s / 10)
        return max(0.0, min(pause, deadline - time.monotonic()))

    async def _attach(self, deadline: float
                      ) -> aiohttp.ClientWebSocketResponse | None:
        """The attached channel, or ``None`` once *deadline* has passed; each
        attempt may take what remains until then."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                async with asyncio.timeout(remaining):
                    return await self._api.attach(self._sid)
            except _NOT_ATTACHED as exc:
                logger.info("live session attach failed: %s", type(exc).__name__)
            await asyncio.sleep(self._retry_pause(deadline))

    async def _close(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async with asyncio.timeout(CLOSE_WAIT_S):
                await ws.close()
        except _NOT_ATTACHED:
            pass

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """Handle events until the channel ends; true when the session was
        closed, false when only the channel dropped."""
        while True:
            msg = await ws.receive()
            if msg.type in _WS_GONE:
                return False
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                if self._on_event(event):
                    return True
            except Exception:  # noqa: BLE001 — one bad event is not the session
                logger.warning("live session event %r could not be handled",
                               event.get("type"), exc_info=True)

    async def _send(self, event: dict) -> None:
        """Send a client event; one that cannot be sent is logged and lost."""
        ws = self._ws
        try:
            if ws is None or ws.closed:
                raise ConnectionError("no control channel")
            await ws.send_json(event)
        except (aiohttp.ClientError, OSError, RuntimeError) as exc:
            logger.warning("live session %s not sent: %s", event["type"],
                           type(exc).__name__)

    async def _append(self, content: str) -> None:
        self._appends += 1
        await self._send({"type": "session.instructions.append",
                          "event_id": f"i_{self._appends}",
                          "delegation_id": None, "content": content})

    async def say(self, text: str) -> None:
        """Have the agent say *text* now."""
        await self._append(SAY_PREFIX + text)

    async def tell(self, text: str) -> None:
        """Give the agent *text* as context; it is not a line to read."""
        await self._append(text)

    # Events.

    def _on_event(self, event: dict) -> bool:
        """Handle one server event without waiting; true for the last one."""
        if self._zero is None:
            self._zero = time.monotonic()
        kind = event.get("type")
        if kind == "session.output_transcript.delta":
            self._on_delta("agent", event)
        elif kind == "session.input_transcript.delta":
            self._on_delta("caller", event)
        elif kind == "session.delegation.created":
            created = event.get("delegation")
            if isinstance(created, dict) and isinstance(created.get("id"), str):
                self._delegations[created["id"]] = self.actions.round
        elif kind == "response.event":
            self._on_helper_event(event)
        elif kind == "session.closed":
            # Pending work and a closed session: the work went through.
            if self.actions.phase == "pending":
                self.actions.resolve_pending(True)
            else:
                self.actions.end()
            self._finish(str(event.get("reason") or "closed"))
            return True
        elif kind == "error":
            error = event.get("error")
            code = error.get("code") or error.get("type") \
                if isinstance(error, dict) else None
            logger.warning("live session error event: %s", str(code)[:80])
        return False

    def _on_delta(self, who: Literal["caller", "agent"], event: dict) -> None:
        text = event.get("delta")
        if not isinstance(text, str) or not text:
            return
        self._deltas += 1
        # Without a time of its own, a delta ends about now.
        end = _ms(event.get("end_ms"),
                  int((time.monotonic() - (self._zero or 0.0)) * 1000))
        start = _ms(event.get("start_ms"), end)
        last = self._lines[-1] if self._lines else None
        if last is not None and last.who == who:
            # A line that so far held only blanks starts at its first word.
            merged = last.text + text if last.text else text.lstrip()
            self._lines[-1] = Line(who, merged, last.start_ms,
                                   max(last.end_ms, end))
        else:
            if who == "caller":
                self.actions.new_round()
            self._lines.append(Line(who, text.lstrip(), start, end))
        if who == "agent":
            if self._first_agent is None:
                self._first_agent = time.monotonic()
            self._agent_end_ms = self._lines[-1].end_ms
            # The text of a line arrives before it has been played to the
            # end; until then the line is not silence.
            self._clock.heard(who, self._agent_line_played())
            self._restart_quiet()
        else:
            self._caller_deltas += 1
            self._clock.heard(who)
            self._cancel_quiet()
        for waiter, wanted in self._speech_waiters.items():
            if wanted in (None, who) and not waiter.done():
                waiter.set_result(None)

    def _agent_spoke_last(self) -> bool:
        return bool(self._lines) and self._lines[-1].who == "agent"

    def _agent_line_played(self) -> float:
        """When the agent's latest line, plus a margin, has been played (a
        ``time.monotonic()`` value)."""
        return (self._zero or 0.0) + self._agent_end_ms / 1000 \
            + self._timing.speak_margin_s

    async def _speech_within(self, seconds: float,
                             who: str | None = None) -> bool:
        """True as soon as *who* (either side when ``None``) speaks, false
        after *seconds*."""
        waiter = asyncio.get_running_loop().create_future()
        self._speech_waiters[waiter] = who
        try:
            async with asyncio.timeout(seconds):
                await waiter
            return True
        except TimeoutError:
            return False
        finally:
            del self._speech_waiters[waiter]

    # The decision trigger.

    def _cancel_quiet(self) -> None:
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None

    def _restart_quiet(self) -> None:
        self._cancel_quiet()
        self._quiet = self._spawn(
            self._decide_when_quiet(self.actions.round, self._deltas), "decision")

    async def _decide_when_quiet(self, round: int, deltas: int) -> None:
        """Wait until the agent has finished saying something, then let the
        decision model ask for an ability in the round the line was said."""
        await asyncio.sleep(self._timing.quiet_s)
        # Asked from here on: later speech no longer cancels this decision,
        # its round judges it.
        self._quiet = None
        choice = await self._ask()
        if choice is decision.FAILED:
            choice = await self._ask()
        if choice is decision.FAILED:
            # Nobody decides for this line. Unless someone spoke since, or
            # accepted work is being awaited, the clock takes over.
            if deltas == self._deltas and self.actions.phase == "active":
                self._clock.undecided()
            return
        if choice is None:
            return
        name, args = choice
        outcome = await self.actions.request(name, args, round=round,
                                             trigger="decision")
        self._acted(name, outcome)
        # The helper hears about its own requests; of this one the agent
        # would otherwise never learn that it did not happen. Only a runner's
        # own words are passed on, never the action state's.
        if outcome.status == "refused" and outcome.detail \
                and self._from_runner.get(id(outcome)) is outcome \
                and round not in self._told:
            self._told.add(round)
            await self.tell(outcome.detail)

    async def _ask(self) -> Any:
        try:
            return await decision.decide(
                self._api, model=self._profile.decision_model,
                instructions=self._profile.decision_instructions,
                lines=tuple(self._lines), abilities=self._profile.abilities,
                timeout_s=self._timing.decision_timeout_s)
        except Exception:  # noqa: BLE001 — counts as a decision not made
            logger.warning("decision step failed", exc_info=True)
            return decision.FAILED

    def _acted(self, name: str, outcome: Outcome) -> None:
        """A final ability that is done has ended the session."""
        if outcome.status == "done" and name in self._final:
            self._finish(CLOSE_REQUESTED)

    # The helper trigger.

    def _on_helper_event(self, event: dict) -> None:
        inner = event.get("event")
        if not isinstance(inner, dict) \
                or inner.get("type") != "response.output_item.done":
            return
        item = inner.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        call_id, name = item.get("call_id"), item.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            return
        delegation_id = event.get("delegation_id")
        round = self._delegations.get(delegation_id) \
            if isinstance(delegation_id, str) else None
        self._spawn(self._helper_call(call_id, name,
                                      parse_arguments(item.get("arguments")),
                                      round),
                    "helper call")

    async def _helper_call(self, call_id: str, name: str, args: dict,
                           round: int | None) -> None:
        """Answer one function call of the helper. *round* is the round its
        delegation started in; a delegation never seen created has none, and
        its call does not run."""
        if round is None:
            logger.info("helper call %s of an unseen delegation was not run", name)
            outcome = Outcome("superseded", "")
        else:
            outcome = await self.actions.request(name, args, round=round,
                                                 trigger="helper")
            self._acted(name, outcome)
        await self._send({
            "type": "response.item.create", "event_id": f"r_{call_id}",
            "item": {"type": "function_call_output", "call_id": call_id,
                     "output": json.dumps({"status": outcome.status,
                                           "detail": outcome.detail})}})
        await self._send({"type": "response.create", "event_id": f"c_{call_id}"})

    # Runners.

    def _noted(self, run: Runner) -> Runner:
        """Wrap a runner so that the outcomes it returns can be told apart
        from the ones the action state answers with itself."""
        async def runner(args: dict, round: int) -> Outcome:
            outcome = await run(args, round)
            self._from_runner[id(outcome)] = outcome
            return outcome
        return runner

    def _after_last_line(self, run: Runner) -> Runner:
        """Wrap a final ability's runner. It runs only when the agent has
        the last word and that line has been played; anything the other side
        says while this waits makes the request stale. A new round always
        starts with such words, so this also covers a newer round."""
        async def runner(args: dict, round: int) -> Outcome:
            heard = self._caller_deltas
            await self._agent_answers(heard)
            if self._agent_spoke_last():
                await self._last_line_played(heard)
            if self._caller_deltas != heard or not self._agent_spoke_last():
                return Outcome("superseded", "")
            return await run(args, round)
        return runner

    async def _agent_answers(self, heard: int) -> None:
        """Wait, at most the grace, until the agent has the last word; no
        longer once the other side has said more than *heard* deltas."""
        limit = time.monotonic() + self._timing.goodbye_grace_s
        while not self._agent_spoke_last() and self._caller_deltas == heard:
            remaining = limit - time.monotonic()
            if remaining <= 0 or not await self._speech_within(remaining):
                return

    async def _last_line_played(self, heard: int) -> None:
        """Wait until the agent's last line, plus a margin, has been played;
        never longer than the grace, and no longer once the other side has
        said more than *heard* deltas."""
        limit = time.monotonic() + self._timing.goodbye_grace_s
        while self._caller_deltas == heard and self._zero is not None:
            wait = min(self._agent_line_played(), limit) - time.monotonic()
            if wait <= 0:
                return
            await self._speech_within(wait, "caller")

    # The clock.

    async def _on_clock(self, which: str) -> bool:
        """A deadline passed; true when the session is over."""
        grace = self._timing.goodbye_grace_s
        if which == "pending":
            self._clock.settled()
            if self.actions.phase == "pending":
                self.actions.resolve_pending(False)
                if self._spoken.get("transfer_failed"):
                    await self.tell(self._spoken["transfer_failed"])
            return False
        if which == "check_in":
            # Context for the agent, which words the question itself. From
            # here on only the other side's speech starts the silence again.
            self._clock.checked_in()
            if self._spoken.get("check_in"):
                await self.tell(self._spoken["check_in"])
            return False
        if which == "silence":
            # Counted from before the line is sent: words of the other side
            # that arrive while it is being sent count like words in the
            # grace.
            heard = self._caller_deltas
            await self._say_line("silence")
            if self._caller_deltas == heard:
                await self._speech_within(grace, "caller")
            if self._caller_deltas != heard:
                return False  # that speech started the silence again
            if self.actions.phase == "pending":
                return False  # work was accepted meanwhile; its clock decides
        elif which == "max":
            await self._say_line("too_long")
            await asyncio.sleep(grace)
        return await self._end(f"clock:{which}")

    async def _say_line(self, key: str) -> None:
        if self._spoken.get(key):
            await self.say(self._spoken[key])


def _reply_gaps(lines: tuple[Line, ...]) -> tuple[int, ...]:
    """For each line of the other side that the agent answered: milliseconds
    from that line's end to the start of the answer (0 when the answer began
    before the line had ended)."""
    return tuple(max(0, after.start_ms - before.end_ms)
                 for before, after in zip(lines, lines[1:])
                 if before.who == "caller" and after.who == "agent")


def _ms(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)
