"""One action state for a live voice session.

Two triggers (a helper model listening to the conversation and a decision
model reading it) may ask for the same ability at nearly the same moment. This
is the single place that decides whether a request runs: at most once per
turn of the other side, never for a turn that is already over, and one at a
time in arrival order.

A turn is a *round*: :meth:`ActionState.new_round` starts the next one when
the other side starts to speak, and every request names the round it was
decided in. The state holds one lock across the runner, so a second request
for the same ability that arrives while the first runs waits and then receives
the stored outcome instead of running again.

An ability whose work was accepted but has not finished yet (handing the
session to someone else, say) calls :meth:`ActionState.mark_pending` from its
runner; until :meth:`ActionState.resolve_pending` settles it, every request for
that ability gets the outcome it already had, and every other ability waits as
``pending``. An ability whose pending work did not complete is spent for the
rest of the session: it was accepted once, so it is never run a second time.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping

logger = logging.getLogger(__name__)

Status = Literal["done", "refused", "superseded", "pending", "final"]
Phase = Literal["active", "pending", "handed_off", "ended"]


@dataclass(frozen=True)
class Outcome:
    status: Status
    detail: str


Runner = Callable[[dict, int], Awaitable[Outcome]]


class ActionState:
    """Decides whether each requested ability runs.

    ``runners`` maps an ability name to ``async (args, round) -> Outcome``.
    An ability in ``final_abilities`` ends the session when it is done.
    """

    def __init__(self, runners: Mapping[str, Runner], *,
                 final_abilities: frozenset[str]) -> None:
        self._runners = dict(runners)
        self._final = frozenset(final_abilities)
        self.round = 1
        self.phase: Phase = "active"
        self.log: list[dict] = []
        self._lock = asyncio.Lock()
        self._settled: dict[tuple[str, int], Outcome] = {}
        self._spent: set[str] = set()
        self._running: tuple[str, int] | None = None
        self._pending: tuple[str, int] | None = None

    # The four state changes below never take the lock: a runner calls
    # mark_pending while request holds it, and the clock may end the session
    # at any moment.

    def new_round(self) -> int:
        """The other side started to speak: requests from earlier rounds are
        stale from now on."""
        self.round += 1
        return self.round

    def end(self) -> None:
        """The session is over (its clock ran out or it was closed), unless
        it was already handed off."""
        if self.phase != "handed_off":
            self.phase = "ended"
            self._pending = None

    def mark_pending(self, ability: str) -> None:
        """The work of ``ability`` was accepted but has not finished. Only an
        active session can wait on such work; an ended one stays ended."""
        if self.phase != "active":
            return
        if self._running is not None and self._running[0] == ability:
            ran_in = self._running[1]
        else:
            ran_in = self.round
        self.phase = "pending"
        self._pending = (ability, ran_in)

    def resolve_pending(self, done: bool) -> None:
        """The pending work finished (``done``) or did not; in the second case
        its ability is spent for the rest of the session."""
        if self.phase != "pending" or self._pending is None:
            return
        ability = self._pending[0]
        self._pending = None
        if done:
            self.phase = "handed_off"
        else:
            self.phase = "active"
            self._spent.add(ability)

    async def request(self, ability: str, args: dict, *, round: int,
                      trigger: str) -> Outcome:
        """Run ``ability`` for ``round`` unless the state says it must not,
        and return what happened."""
        async with self._lock:
            outcome = await self._decide(ability, args, round, trigger)
        self._record(ability, trigger, round, outcome)
        return outcome

    def _record(self, ability: str, trigger: str, round: int,
                outcome: Outcome) -> None:
        self.log.append({"ability": ability, "trigger": trigger,
                         "round": round, "status": outcome.status,
                         "t": time.monotonic()})

    async def _decide(self, ability: str, args: dict, round: int,
                      trigger: str) -> Outcome:
        runner = self._runners.get(ability)
        if runner is None:
            return Outcome("refused", "unknown ability")
        if self.phase in ("handed_off", "ended"):
            return Outcome("final", self.phase)
        if self.phase == "pending" and self._pending is not None:
            pending, ran_in = self._pending
            if ability != pending:
                return Outcome("pending", pending)
            # Both triggers see the same result for the one piece of work in
            # flight, whichever round they decided in.
            return self._settled.get((pending, ran_in),
                                     Outcome("pending", pending))
        if round < self.round:
            return Outcome("superseded", "")
        if ability in self._spent:
            return Outcome("refused", "already tried")
        settled = self._settled.get((ability, round))
        if settled is not None:
            return settled
        return await self._run(ability, runner, args, round, trigger)

    async def _run(self, ability: str, runner: Runner, args: dict,
                   round: int, trigger: str) -> Outcome:
        self._running = (ability, round)
        try:
            outcome = await runner(args, round)
        except asyncio.CancelledError:
            # The runner's work may already have left, so the round counts as
            # settled: a later request in this round must not run it again.
            interrupted = Outcome("refused", "interrupted")
            self._settled[(ability, round)] = interrupted
            self._record(ability, trigger, round, interrupted)
            raise
        except Exception:  # noqa: BLE001 — any failure is a refusal
            logger.warning("action %s failed", ability, exc_info=True)
            outcome = Outcome("refused", "failed")
        finally:
            self._running = None
        # A runner that found its request stale tells the requester so, but
        # that is no answer for the round: a later request may still run.
        if outcome.status == "superseded":
            return outcome
        self._settled[(ability, round)] = outcome
        # A runner may have handed the session off itself; that outlasts
        # its own "done".
        if outcome.status == "done" and ability in self._final \
                and self.phase != "handed_off":
            self.phase = "ended"
            self._pending = None
        return outcome
