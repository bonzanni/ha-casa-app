"""#1350: one progress line per configurator turn, opened at the turn's first
tool use, edited as steps change, closed by the same turn."""
from __future__ import annotations

import asyncio

import pytest

from drivers.turn_progress import TurnProgressLine

pytestmark = [pytest.mark.unit]


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Wire:
    def __init__(self, *, send_ok: bool = True, hang: bool = False) -> None:
        self.sent: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.send_ok = send_ok
        self.hang = hang

    async def send(self, text: str):
        if self.hang:
            await asyncio.Event().wait()
        self.sent.append(text)
        return 42 if self.send_ok else None

    async def edit(self, message_id: int, text: str) -> bool:
        if self.hang:
            await asyncio.Event().wait()
        self.edits.append((message_id, text))
        return True


async def _no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


def _line(wire, clock, **kw):
    return TurnProgressLine(send=wire.send, edit=wire.edit, now=clock,
                            sleep=kw.pop("sleep", _no_sleep), **kw)


async def test_no_tool_use_posts_nothing():
    from drivers.turn_progress import _closing
    wire, clock = _Wire(), _Clock()
    line = _line(wire, clock)
    line.finish(ok=True)
    assert not _closing
    await line.close(ok=True)
    assert wire.sent == [] and wire.edits == []


async def test_first_step_posts_and_close_edits_the_summary():
    wire, clock = _Wire(), _Clock()
    line = _line(wire, clock)
    line.step("reading files")
    await asyncio.sleep(0)
    clock.t = 72
    await line.close(ok=True)
    assert wire.sent == ["🔧 This turn so far: 1 step — latest: reading files (at 0s)"]
    assert wire.edits[-1] == (42, "☑ This turn: 1 step in 1m 12s")


async def test_steps_coalesce_to_the_newest_and_count():
    wire, clock = _Wire(), _Clock()
    line = _line(wire, clock)
    line.step("reading files")
    line.step("updating plugins")
    clock.t = 5
    line.step("committing the change")
    for _ in range(5):
        await asyncio.sleep(0)
    clock.t = 30
    await line.close(ok=True)
    # three steps before the line's first write: only the newest is shown
    assert wire.sent == ["🔧 This turn so far: 3 steps — latest: committing the change (at 5s)"]
    assert wire.edits == [(42, "☑ This turn: 3 steps in 30s")]


async def test_a_raised_turn_closes_as_stopped():
    wire, clock = _Wire(), _Clock()
    line = _line(wire, clock)
    line.step("reading files")
    line.step("editing files")
    await asyncio.sleep(0)
    await line.close(ok=False)
    assert wire.edits[-1] == (42, "✖ This turn stopped after 2 steps")


async def test_a_failed_post_leaves_nothing_to_close():
    wire, clock = _Wire(send_ok=False), _Clock()
    line = _line(wire, clock)
    line.step("reading files")
    await asyncio.sleep(0)
    await line.close(ok=True)
    assert wire.edits == []


async def test_a_hung_transport_never_holds_the_turn():
    wire, clock = _Wire(hang=True), _Clock()
    line = _line(wire, clock, io_timeout_s=0.05)
    line.step("reading files")
    await asyncio.wait_for(line.close(ok=True), timeout=1.0)
    assert wire.sent == [] and wire.edits == []


async def test_edits_are_spaced():
    wire, clock = _Wire(), _Clock()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)
        clock.t += s
        await asyncio.sleep(0)

    line = _line(wire, clock, sleep=sleep, spacing_s=3.0)
    clock.t = 0.5                    # the first post lands AFTER the line's start…
    line.step("reading files")
    # wait for that post to have landed before the clock moves: await the pump
    # itself, never a count of loop turns — Python 3.11's wait_for runs the send
    # in a task of its own and needs more turns than 3.12's (main QA, v0.344.59)
    await line._pump
    assert wire.sent == ["🔧 This turn so far: 1 step — latest: reading files (at 0s)"]
    clock.t = 1.0
    line.step("editing files")
    await line._pump
    await line.close(ok=True)
    # …so the spacing is counted from that write (3.0 - 0.5), not from the start,
    # and the close waits a full spacing after the edit that landed at 3.5
    assert waits == [pytest.approx(2.5), pytest.approx(3.0)]
    assert wire.edits == [(42, "🔧 This turn so far: 2 steps — latest: editing files (at 1s)"), (42, "☑ This turn: 2 steps in 3s")]


async def test_the_closing_edit_keeps_the_spacing():
    """#1350 x1 (Astra): the close waits out the spacing like any write."""
    wire, clock = _Wire(), _Clock()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)
        clock.t += s
        await asyncio.sleep(0)

    line = _line(wire, clock, sleep=sleep, spacing_s=3.0)
    line.step("reading files")
    await asyncio.sleep(0)
    await line.close(ok=True)
    assert waits == [pytest.approx(3.0)]
    assert wire.edits == [(42, "☑ This turn: 1 step in 0s")]


async def test_finish_closes_in_the_background():
    from drivers.turn_progress import _closing
    wire, clock = _Wire(), _Clock()
    line = _line(wire, clock, spacing_s=0.0)
    line.step("reading files")
    line.finish(ok=False)
    await asyncio.gather(*list(_closing))
    assert wire.edits[-1] == (42, "✖ This turn stopped after 1 step")


async def test_a_raising_send_is_contained_in_the_pump():
    async def boom(*_a, **_k):
        raise RuntimeError("telegram down")
    clock = _Clock()
    line = TurnProgressLine(send=boom, edit=boom, now=clock, sleep=_no_sleep)
    line.step("reading files")
    await line._pump                 # raises here if the error escaped
    assert line._pump.exception() is None
    await line.close(ok=True)


async def test_a_raising_edit_is_contained_in_the_pump_and_the_close():
    sent: list[str] = []

    async def send(text):
        sent.append(text)
        return 42

    async def boom(*_a, **_k):
        raise RuntimeError("telegram down")
    clock = _Clock()
    line = TurnProgressLine(send=send, edit=boom, now=clock, sleep=_no_sleep,
                            spacing_s=0.0)
    line.step("reading files")
    await line._pump
    line.step("editing files")
    await line._pump
    assert line._pump.exception() is None
    await line.close(ok=True)        # the closing edit raises inside; contained
    assert sent == ["🔧 This turn so far: 1 step — latest: reading files (at 0s)"]


async def test_the_closing_duration_is_the_turns_not_the_delivery_delay():
    """#1350 x2 (Astra): finish() freezes the turn's elapsed time."""
    from drivers.turn_progress import _closing
    wire, clock = _Wire(), _Clock()

    async def sleep(s: float) -> None:
        clock.t += s
        await asyncio.sleep(0)

    line = _line(wire, clock, sleep=sleep, spacing_s=3.0)
    line.step("reading files")
    await asyncio.sleep(0)
    clock.t = 1.0
    line.finish(ok=True)
    clock.t = 11.0                   # the close runs later; the turn took 1s
    await asyncio.gather(*list(_closing))
    assert wire.edits == [(42, "☑ This turn: 1 step in 1s")]


async def test_a_timed_out_drain_still_spaces_the_closing_edit():
    """#1350 x2 (Astra): the cancelled pump is settled before spacing."""
    clock = _Clock()
    starts: list[tuple[str, float]] = []
    hang_next = {"on": False}

    async def send(text):
        starts.append(("send", clock.t))
        return 42

    async def edit(mid, text):
        starts.append(("edit", clock.t))
        if hang_next["on"]:
            hang_next["on"] = False
            await asyncio.Event().wait()
        return True

    async def sleep(s: float) -> None:
        clock.t += s
        await asyncio.sleep(0)

    line = TurnProgressLine(send=send, edit=edit, now=clock, sleep=sleep,
                            spacing_s=3.0, io_timeout_s=0.01)
    line.step("reading files")
    await asyncio.sleep(0)
    clock.t = 1.0
    hang_next["on"] = True
    line.step("editing files")
    await line.close(ok=True)
    edits = [t for k, t in starts if k == "edit"]
    assert len(edits) == 2 and edits[1] - edits[0] >= 3.0
