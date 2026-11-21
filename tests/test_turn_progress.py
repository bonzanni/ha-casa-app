"""The configurator's progress line: a short list of rows, one per activity,
each settled through its own handle, in one message deleted when the run ends."""
from __future__ import annotations

import asyncio

import pytest

from drivers.turn_progress import MAX_ROWS, TurnProgressLine, activity_for_tool

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
        self.deleted: list[int] = []
        self._texts: list[str] = []
        self.send_ok = send_ok
        self.hang = hang

    async def send(self, text: str):
        if self.hang:
            await asyncio.Event().wait()
        self.sent.append(text)
        self._texts.append(text)
        return 42 if self.send_ok else None

    async def edit(self, message_id: int, text: str) -> bool:
        if self.hang:
            await asyncio.Event().wait()
        self.edits.append((message_id, text))
        self._texts.append(text)
        return True

    async def delete(self, message_id: int) -> bool:
        if self.hang:
            await asyncio.Event().wait()
        self.deleted.append(message_id)
        return True

    @property
    def last(self) -> str | None:
        """The newest text sent or edited in."""
        return self._texts[-1] if self._texts else None


async def _no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


async def _settle() -> None:
    """Yield to the loop long enough for the background writes to land."""
    for _ in range(50):
        await asyncio.sleep(0)


def _line(wire, clock=None, **kw):
    return TurnProgressLine(send=wire.send, edit=wire.edit, delete=wire.delete,
                            now=clock or _Clock(), sleep=kw.pop("sleep", _no_sleep),
                            **kw)


async def test_no_row_posts_nothing():
    from drivers.turn_progress import _closing
    wire = _Wire()
    line = _line(wire)
    line.finish()
    assert not _closing
    await _settle()
    assert wire.sent == [] and wire.deleted == []


async def test_a_row_runs_then_ticks():
    wire = _Wire()
    line = _line(wire)
    r = line.row("Download and install Gmail")
    await _settle()
    assert wire.sent == ["⏳ Download and install Gmail"]
    r.done()
    await _settle()
    assert wire.last == "✓ Download and install Gmail"


async def test_a_failed_row_is_crossed():
    wire = _Wire()
    line = _line(wire)
    r = line.row("Download and install Gmail")
    await _settle()
    r.failed()
    await _settle()
    assert wire.last == "✗ Download and install Gmail"


async def test_rows_are_independent():
    wire = _Wire()
    line = _line(wire)
    a = line.row("A")
    b = line.row("B")
    b.done()
    await _settle()
    assert wire.last == "⏳ A\n✓ B"
    assert a is not b


async def test_rows_made_before_the_first_write_coalesce_into_one_post():
    wire = _Wire()
    line = _line(wire)
    line.row("A")
    line.row("B").done()
    line.row("C")
    await _settle()
    assert wire.sent == ["⏳ A\n✓ B\n⏳ C"] and wire.edits == []


async def test_a_row_not_kept_disappears_when_settled():
    wire = _Wire()
    line = _line(wire)
    a = line.row("Work on it", keep=False)
    line.row("B")
    a.done()
    await _settle()
    assert wire.last == "⏳ B"


async def test_the_last_row_disappearing_deletes_the_message_and_a_later_row_posts_again():
    wire = _Wire()
    line = _line(wire)
    a = line.row("Work on it", keep=False)
    await _settle()
    a.done()
    await _settle()
    assert wire.deleted == [42] and (42, "") not in wire.edits
    line.row("B")
    await _settle()
    assert wire.sent == ["⏳ Work on it", "⏳ B"]


async def test_a_row_never_settled_stays_running_and_the_message_is_deleted():
    from drivers.turn_progress import _closing
    wire = _Wire()
    line = _line(wire)
    line.row("A")
    await _settle()
    line.finish()
    await asyncio.gather(*list(_closing))
    assert wire.last == "⏳ A" and wire.deleted == [42]


async def test_only_the_last_eight_rows_show():
    wire = _Wire()
    line = _line(wire)
    labels = [f"{i:02d}" + "x" * 58 for i in range(12)]
    for label in labels:
        line.row(label).done()
    await _settle()
    lines = wire.last.split("\n")
    assert MAX_ROWS == 8
    assert lines == [f"✓ {label}" for label in labels[-8:]]
    assert len(wire.last) < 4096


async def test_settling_twice_is_ignored():
    wire = _Wire()
    line = _line(wire)
    r = line.row("A")
    r.done()
    r.failed()
    await _settle()
    assert wire.last == "✓ A"


async def test_a_row_settled_after_the_close_posts_nothing_more():
    from drivers.turn_progress import _closing
    wire = _Wire()
    line = _line(wire)
    r = line.row("A")
    await _settle()
    line.finish()
    await asyncio.gather(*list(_closing))
    r.done()
    line.row("B")
    await _settle()
    assert wire.sent == ["⏳ A"] and wire.edits == [] and wire.deleted == [42]


async def test_a_failed_post_leaves_nothing_to_close():
    wire = _Wire(send_ok=False)
    line = _line(wire)
    line.row("A")
    await _settle()
    await line.close()
    assert wire.edits == [] and wire.deleted == []


async def test_a_hung_transport_never_holds_the_turn():
    wire = _Wire(hang=True)
    line = _line(wire, io_timeout_s=0.05)
    line.row("A")
    await asyncio.wait_for(line.close(), timeout=1.0)
    assert wire.sent == [] and wire.edits == [] and wire.deleted == []


async def test_edits_are_spaced():
    wire, clock = _Wire(), _Clock()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)
        clock.t += s
        await asyncio.sleep(0)

    line = _line(wire, clock, sleep=sleep, spacing_s=3.0)
    clock.t = 0.5                    # the first post lands AFTER the line's start…
    a = line.row("A")
    # wait for that post to have landed before the clock moves: await the pump
    # itself, never a count of loop turns (wait_for's task count differs by
    # Python version)
    await line._pump
    assert wire.sent == ["⏳ A"]
    clock.t = 1.0
    a.done()
    await line._pump
    await line.close()
    # …so the spacing is counted from that write (3.0 - 0.5), not from the start,
    # and the closing delete waits a full spacing after the edit that landed at 3.5
    assert waits == [pytest.approx(2.5), pytest.approx(3.0)]
    assert wire.edits == [(42, "✓ A")] and wire.deleted == [42]


async def test_the_closing_delete_keeps_the_spacing():
    wire, clock = _Wire(), _Clock()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)
        clock.t += s
        await asyncio.sleep(0)

    line = _line(wire, clock, sleep=sleep, spacing_s=3.0)
    line.row("A")
    await line._pump
    await line.close()
    assert waits == [pytest.approx(3.0)]
    assert wire.edits == [] and wire.deleted == [42]


async def test_finish_closes_in_the_background():
    from drivers.turn_progress import _closing
    wire = _Wire()
    line = _line(wire, spacing_s=0.0)
    line.row("A")
    line.finish()
    assert _closing
    await asyncio.gather(*list(_closing))
    assert wire.sent == ["⏳ A"] and wire.deleted == [42]


async def test_a_raising_send_is_contained_in_the_pump():
    async def boom(*_a, **_k):
        raise RuntimeError("telegram down")
    line = TurnProgressLine(send=boom, edit=boom, delete=boom, now=_Clock(),
                            sleep=_no_sleep)
    line.row("A")
    await line._pump                 # raises here if the error escaped
    assert line._pump.exception() is None
    await line.close()


async def test_a_raising_edit_is_contained_in_the_pump_and_the_close():
    sent: list[str] = []

    async def send(text):
        sent.append(text)
        return 42

    async def boom(*_a, **_k):
        raise RuntimeError("telegram down")
    line = TurnProgressLine(send=send, edit=boom, delete=boom, now=_Clock(),
                            sleep=_no_sleep, spacing_s=0.0)
    a = line.row("A")
    await line._pump
    a.done()
    await line._pump
    assert line._pump.exception() is None
    await line.close()               # the closing delete raises inside; contained
    assert sent == ["⏳ A"]


async def test_a_timed_out_drain_still_spaces_the_closing_delete():
    """The cancelled pump is settled before the spacing is counted."""
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

    async def delete(mid):
        starts.append(("delete", clock.t))
        return True

    async def sleep(s: float) -> None:
        clock.t += s
        await asyncio.sleep(0)

    line = TurnProgressLine(send=send, edit=edit, delete=delete, now=clock,
                            sleep=sleep, spacing_s=3.0, io_timeout_s=0.01)
    a = line.row("A")
    await line._pump
    clock.t = 1.0
    hang_next["on"] = True
    a.done()
    await line.close()
    edits = [t for k, t in starts if k == "edit"]
    deletes = [t for k, t in starts if k == "delete"]
    assert len(edits) == 1 and len(deletes) == 1
    assert deletes[0] - edits[0] >= 3.0


# Moved from tests/test_summary_controller.py with the helpers they pin.
class TestActivityMapping:
    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("Read", "reading files"),
            ("Glob", "reading files"),
            ("Grep", "reading files"),
            ("Write", "editing files"),
            ("Edit", "editing files"),
            ("NotebookEdit", "editing files"),
            ("Bash", "running commands"),
            ("Task", "planning"),
            ("TaskOutput", "planning"),
            ("TodoWrite", "planning"),
            ("WebFetch", "researching"),
            ("WebSearch", "researching"),
            ("mcp__ha-control__call_service", "using ha-control tools"),
            ("mcp__plugin_bank-feed_bank-feed__daily_sync",
             "using plugin_bank-feed_bank-feed tools"),
            ("mcp__casa-framework__emit_completion", "wrapping up"),
            # #1350: Casa's own tools in the operator's words
            ("mcp__casa-framework__plugin_update", "updating plugins"),
            ("mcp__casa-framework__plugin_list", "updating plugins"),
            ("mcp__casa-framework__config_git_commit", "committing the change"),
            ("mcp__casa-framework__casa_reload", "reloading Casa"),
            ("mcp__casa-framework__casa_reload_triggers", "reloading Casa"),
            ("mcp__casa-framework__casa_restart_supervised", "reloading Casa"),
            ("mcp__casa-framework__specialist_upgrade", "managing specialists"),
            ("mcp__casa-framework__persona_apply", "managing personas"),
            ("mcp__casa-framework__resident_persona_swap", "managing personas"),
            ("mcp__casa-framework__list_vault_items", "wiring secrets"),
            ("mcp__casa-framework__get_item_fields", "wiring secrets"),
            ("mcp__casa-framework__set_plugin_env_reference", "wiring secrets"),
            ("mcp__casa-framework__remove_plugin_env_reference", "wiring secrets"),
            ("mcp__casa-framework__config_trigger_upsert", "updating triggers"),
            ("mcp__casa-framework__query_engager", "using casa-framework tools"),
            ("SomethingElse", "working"),
            ("", "working"),
        ],
    )
    def test_coarse_mapping(self, tool, expected):
        assert activity_for_tool(tool) == expected

    def test_mcp_server_name_capped_at_32(self):
        # P1-B r4: the mcp server substring is capped at 32 chars AT SOURCE so
        # the status line cannot blow the 4096 wire bound.
        long = "s" * 100
        out = activity_for_tool(f"mcp__{long}__do")
        assert out == "using " + "s" * 32 + " tools"
