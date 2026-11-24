"""The action state shared by the two triggers of a live voice session.

Runners are small async functions that count their calls; a runner that must
stay in flight waits on an ``asyncio.Event``.
"""
from __future__ import annotations

import asyncio

import pytest


from live_voice.actions import ActionState, Outcome


class Runner:
    """Counts calls and answers with a fixed outcome (or raises it); an
    optional gate holds every call until it is set, and an optional hook runs
    before the answer."""

    def __init__(self, answer: Outcome | Exception = Outcome("done", "ok"),
                 *, gate: asyncio.Event | None = None, hook=None) -> None:
        self.answer = answer
        self.gate = gate
        self.hook = hook
        self.calls: list[tuple[dict, int]] = []

    async def __call__(self, args: dict, round: int) -> Outcome:
        self.calls.append((args, round))
        if self.gate is not None:
            await self.gate.wait()
        if self.hook is not None:
            self.hook()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _state(transfer: Runner, end_call: Runner | None = None) -> ActionState:
    return ActionState({"transfer": transfer,
                        "end_call": end_call or Runner()},
                       final_abilities=frozenset({"end_call"}))


async def test_starts_in_round_one_and_active():
    state = _state(Runner())
    assert state.round == 1
    assert state.phase == "active"
    assert state.log == []


async def test_new_round_counts_up():
    state = _state(Runner())
    assert state.new_round() == 2
    assert state.new_round() == 3
    assert state.round == 3


async def test_unknown_ability_is_refused_without_running():
    transfer = Runner()
    state = _state(transfer)
    out = await state.request("sing", {}, round=1, trigger="helper")
    assert out == Outcome("refused", "unknown ability")
    assert transfer.calls == []


async def test_same_ability_twice_in_one_round_runs_once():
    transfer = Runner()
    state = _state(transfer)
    first = await state.request("transfer", {"a": 1}, round=1,
                                trigger="helper")
    second = await state.request("transfer", {"a": 2}, round=1,
                                 trigger="decider")
    assert len(transfer.calls) == 1
    assert transfer.calls[0] == ({"a": 1}, 1)
    assert first == second == Outcome("done", "ok")


async def test_refused_ability_stays_refused_in_its_round_others_still_run():
    transfer = Runner(Outcome("refused", "nobody to put through"))
    end_call = Runner()
    state = _state(transfer, end_call)
    first = await state.request("transfer", {}, round=1, trigger="helper")
    again = await state.request("transfer", {}, round=1, trigger="decider")
    assert first == again == Outcome("refused", "nobody to put through")
    assert len(transfer.calls) == 1
    out = await state.request("end_call", {}, round=1, trigger="decider")
    assert out == Outcome("done", "ok")
    assert len(end_call.calls) == 1


async def test_a_new_round_lets_the_ability_run_again():
    transfer = Runner(Outcome("refused", "not now"))
    state = _state(transfer)
    await state.request("transfer", {}, round=1, trigger="helper")
    current = state.new_round()
    await state.request("transfer", {}, round=current, trigger="helper")
    assert [r for _, r in transfer.calls] == [1, 2]


async def test_a_request_from_an_older_round_is_superseded():
    transfer = Runner()
    state = _state(transfer)
    state.new_round()
    out = await state.request("transfer", {}, round=1, trigger="decider")
    assert out == Outcome("superseded", "")
    assert transfer.calls == []
    assert state.phase == "active"


async def test_concurrent_requests_run_once_and_share_the_outcome():
    gate = asyncio.Event()
    transfer = Runner(gate=gate)
    state = _state(transfer)
    both = asyncio.gather(
        state.request("transfer", {}, round=1, trigger="helper"),
        state.request("transfer", {}, round=1, trigger="decider"))
    await asyncio.sleep(0.01)
    assert len(transfer.calls) == 1
    gate.set()
    first, second = await both
    assert first == second == Outcome("done", "ok")
    assert len(transfer.calls) == 1


async def test_after_a_final_ability_is_done_every_request_is_final():
    transfer = Runner()
    end_call = Runner()
    state = _state(transfer, end_call)
    out = await state.request("end_call", {}, round=1, trigger="helper")
    assert out == Outcome("done", "ok")
    assert state.phase == "ended"
    final = Outcome("final", "ended")
    assert await state.request("transfer", {}, round=1,
                               trigger="decider") == final
    assert await state.request("end_call", {}, round=1,
                               trigger="decider") == final
    current = state.new_round()
    assert await state.request("transfer", {}, round=current,
                               trigger="decider") == final
    assert transfer.calls == []
    assert len(end_call.calls) == 1


async def test_a_final_ability_that_is_refused_does_not_end():
    end_call = Runner(Outcome("refused", "no"))
    state = _state(Runner(), end_call)
    await state.request("end_call", {}, round=1, trigger="helper")
    assert state.phase == "active"


async def test_pending_then_resolved_false_spends_the_ability():
    state: ActionState
    transfer = Runner(hook=lambda: state.mark_pending("transfer"))
    end_call = Runner()
    state = _state(transfer, end_call)

    first = await state.request("transfer", {}, round=1, trigger="helper")
    assert first == Outcome("done", "ok")
    assert state.phase == "pending"

    second = await state.request("transfer", {}, round=1, trigger="decider")
    assert second == first
    assert len(transfer.calls) == 1
    other = await state.request("end_call", {}, round=1, trigger="decider")
    assert other == Outcome("pending", "transfer")
    assert end_call.calls == []

    state.resolve_pending(False)
    assert state.phase == "active"
    same_round = await state.request("transfer", {}, round=1,
                                     trigger="helper")
    assert same_round == Outcome("refused", "already tried")
    current = state.new_round()
    later = await state.request("transfer", {}, round=current,
                                trigger="decider")
    assert later == Outcome("refused", "already tried")
    assert len(transfer.calls) == 1

    out = await state.request("end_call", {}, round=current, trigger="helper")
    assert out == Outcome("done", "ok")
    assert len(end_call.calls) == 1


async def test_pending_resolved_true_hands_off():
    state: ActionState
    transfer = Runner(hook=lambda: state.mark_pending("transfer"))
    end_call = Runner()
    state = _state(transfer, end_call)
    await state.request("transfer", {}, round=1, trigger="helper")
    state.resolve_pending(True)
    assert state.phase == "handed_off"
    assert await state.request("end_call", {}, round=1,
                               trigger="decider") == Outcome("final",
                                                             "handed_off")
    assert end_call.calls == []


async def test_pending_returns_the_stored_outcome_from_any_round():
    state: ActionState
    transfer = Runner(hook=lambda: state.mark_pending("transfer"))
    state = _state(transfer)
    first = await state.request("transfer", {}, round=1, trigger="helper")
    state.new_round()
    stale = await state.request("transfer", {}, round=1, trigger="decider")
    fresh = await state.request("transfer", {}, round=2, trigger="decider")
    assert stale == fresh == first
    assert len(transfer.calls) == 1


async def test_a_request_waiting_on_a_pending_runner_sees_pending():
    gate = asyncio.Event()
    state: ActionState
    transfer = Runner(gate=gate, hook=lambda: state.mark_pending("transfer"))
    end_call = Runner()
    state = _state(transfer, end_call)
    results = asyncio.gather(
        state.request("transfer", {}, round=1, trigger="helper"),
        state.request("transfer", {}, round=1, trigger="decider"),
        state.request("end_call", {}, round=1, trigger="decider"))
    await asyncio.sleep(0.01)
    gate.set()
    first, second, other = await results
    assert first == second == Outcome("done", "ok")
    assert other == Outcome("pending", "transfer")
    assert len(transfer.calls) == 1
    assert end_call.calls == []


async def test_resolve_pending_outside_pending_does_nothing():
    state = _state(Runner())
    state.resolve_pending(True)
    assert state.phase == "active"
    state.resolve_pending(False)
    assert state.phase == "active"
    out = await state.request("transfer", {}, round=1, trigger="helper")
    assert out == Outcome("done", "ok")


async def test_a_raising_runner_is_refused_and_state_stays_active():
    transfer = Runner(RuntimeError("boom"))
    state = _state(transfer)
    out = await state.request("transfer", {}, round=1, trigger="helper")
    assert out == Outcome("refused", "failed")
    assert state.phase == "active"
    again = await state.request("transfer", {}, round=1, trigger="decider")
    assert again == Outcome("refused", "failed")
    assert len(transfer.calls) == 1


async def test_a_superseded_runner_outcome_is_returned_but_not_stored():
    transfer = Runner(Outcome("superseded", "stale"))
    state = _state(transfer)
    out = await state.request("transfer", {}, round=1, trigger="helper")
    assert out == Outcome("superseded", "stale")
    assert state.phase == "active"
    transfer.answer = Outcome("done", "ok")
    again = await state.request("transfer", {}, round=1, trigger="decider")
    assert again == Outcome("done", "ok")
    assert len(transfer.calls) == 2


async def test_end_ends_unless_handed_off():
    state = _state(Runner())
    state.end()
    assert state.phase == "ended"
    assert await state.request("transfer", {}, round=1,
                               trigger="helper") == Outcome("final", "ended")


async def test_end_while_pending_ends():
    state: ActionState
    transfer = Runner(hook=lambda: state.mark_pending("transfer"))
    state = _state(transfer)
    await state.request("transfer", {}, round=1, trigger="helper")
    state.end()
    assert state.phase == "ended"
    state.resolve_pending(True)
    assert state.phase == "ended"


async def test_end_after_hand_off_stays_handed_off():
    state: ActionState
    transfer = Runner(hook=lambda: state.mark_pending("transfer"))
    state = _state(transfer)
    await state.request("transfer", {}, round=1, trigger="helper")
    state.resolve_pending(True)
    state.end()
    assert state.phase == "handed_off"


async def test_mark_pending_after_end_does_not_reopen_the_session():
    state: ActionState
    transfer = Runner(hook=lambda: (state.end(),
                                    state.mark_pending("transfer")))
    state = _state(transfer)
    await state.request("transfer", {}, round=1, trigger="helper")
    assert state.phase == "ended"


async def test_the_log_has_one_entry_per_request_with_its_trigger():
    transfer = Runner()
    state = _state(transfer)
    await state.request("transfer", {}, round=1, trigger="helper")
    await state.request("transfer", {}, round=1, trigger="decider")
    await state.request("sing", {}, round=1, trigger="decider")
    state.new_round()
    await state.request("end_call", {}, round=1, trigger="helper")
    assert [(e["ability"], e["trigger"], e["round"], e["status"])
            for e in state.log] == [
        ("transfer", "helper", 1, "done"),
        ("transfer", "decider", 1, "done"),
        ("sing", "decider", 1, "refused"),
        ("end_call", "helper", 1, "superseded"),
    ]
    times = [e["t"] for e in state.log]
    assert times == sorted(times)
    assert all(isinstance(t, float) for t in times)


async def test_an_unknown_ability_is_refused_even_after_the_end():
    state = _state(Runner())
    state.end()
    out = await state.request("sing", {}, round=1, trigger="helper")
    assert out == Outcome("refused", "unknown ability")


async def test_a_request_that_waited_through_a_new_round_is_superseded():
    gate = asyncio.Event()
    transfer = Runner(gate=gate)
    end_call = Runner()
    state = _state(transfer, end_call)
    results = asyncio.gather(
        state.request("transfer", {}, round=1, trigger="helper"),
        state.request("end_call", {}, round=1, trigger="decider"))
    await asyncio.sleep(0.01)
    state.new_round()
    gate.set()
    first, queued = await results
    assert first == Outcome("done", "ok")
    assert queued == Outcome("superseded", "")
    assert end_call.calls == []
    assert state.phase == "active"


async def test_a_cancelled_request_still_settles_its_round():
    gate = asyncio.Event()
    transfer = Runner(gate=gate)
    state = _state(transfer)
    task = asyncio.create_task(
        state.request("transfer", {}, round=1, trigger="helper"))
    await asyncio.sleep(0.01)
    assert len(transfer.calls) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    again = await state.request("transfer", {}, round=1, trigger="decider")
    assert again == Outcome("refused", "interrupted")
    assert len(transfer.calls) == 1
    assert [(e["trigger"], e["status"]) for e in state.log] == [
        ("helper", "refused"), ("decider", "refused")]


async def test_pending_is_tied_to_the_round_the_runner_started_in():
    gate = asyncio.Event()
    state: ActionState
    transfer = Runner(gate=gate, hook=lambda: state.mark_pending("transfer"))
    state = _state(transfer)
    first = asyncio.create_task(
        state.request("transfer", {}, round=1, trigger="helper"))
    await asyncio.sleep(0.01)
    current = state.new_round()
    gate.set()
    assert await first == Outcome("done", "ok")
    assert state.phase == "pending"
    old = await state.request("transfer", {}, round=1, trigger="decider")
    new = await state.request("transfer", {}, round=current,
                              trigger="decider")
    assert old == new == Outcome("done", "ok")
    assert len(transfer.calls) == 1


async def test_pending_then_superseded_answers_pending_without_running():
    state: ActionState
    transfer = Runner(Outcome("superseded", "stale"),
                      hook=lambda: state.mark_pending("transfer"))
    state = _state(transfer)
    out = await state.request("transfer", {}, round=1, trigger="helper")
    assert out == Outcome("superseded", "stale")
    assert state.phase == "pending"
    again = await state.request("transfer", {}, round=1, trigger="decider")
    assert again == Outcome("pending", "transfer")
    assert len(transfer.calls) == 1


async def test_a_final_ability_that_hands_off_itself_stays_handed_off():
    state: ActionState

    def hand_off() -> None:
        state.mark_pending("end_call")
        state.resolve_pending(True)

    end_call = Runner(hook=hand_off)
    state = _state(Runner(), end_call)
    out = await state.request("end_call", {}, round=1, trigger="helper")
    assert out == Outcome("done", "ok")
    assert state.phase == "handed_off"
