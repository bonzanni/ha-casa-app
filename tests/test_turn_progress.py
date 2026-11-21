"""The configurator's progress line: a short list of rows, one per activity,
each settled through its own handle, in one message deleted when the run ends."""
from __future__ import annotations

import asyncio

import pytest

from pathlib import Path

import yaml

from drivers.turn_progress import (
    GENERIC_LABEL, MAX_ROWS, STAGE_ROW_TOOLS, TurnProgressLine, plugin_label,
    step_label, target_label,
)

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


async def test_a_row_that_comes_and_goes_during_the_spacing_deletes_nothing():
    """After the message is deleted, a keep=False row that starts and settles
    while the pump waits out the spacing has no message to delete; a later row
    still posts."""
    class _StrictWire(_Wire):
        async def delete(self, message_id):
            if message_id is None:
                raise TypeError("no message id")
            return await super().delete(message_id)

    wire = _StrictWire()
    gate = asyncio.Event()

    async def sleep(_s: float) -> None:
        await gate.wait()

    line = _line(wire, sleep=sleep)
    a = line.row("Work on it", keep=False)
    await _settle()
    a.done()                         # the pump waits out the spacing at the gate
    gate.set()
    await _settle()
    assert wire.deleted == [42]
    gate.clear()
    b = line.row("Work on it", keep=False)
    await _settle()                  # the pump is waiting out the spacing again
    b.done()
    gate.set()
    await _settle()
    line.row("C")
    await _settle()
    assert wire.deleted == [42]
    assert wire.sent == ["⏳ Work on it", "⏳ C"]


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


# --- the labels -----------------------------------------------------------

_CASA = "mcp__casa-framework__"
_NAMES = {"finance": "Alex", "assistant": "Ellen"}


def _agent_name(role: str) -> str:
    return _NAMES.get(role, role)


def _label(tool: str, args: dict | None = None) -> str | None:
    return step_label(tool, args or {}, agent_name=_agent_name)


@pytest.mark.parametrize("tool,args,expected", [
    ("plugin_assign", {"name": "gmail", "target": "specialist:finance"},
     "Set up Gmail for Alex"),
    ("plugin_unassign", {"name": "gmail", "target": "resident:assistant"},
     "Take Gmail away from Ellen"),
    ("plugin_remove", {"name": "todo-list"}, "Remove Todo List"),
    ("verify_plugin_state", {"plugin_name": "gmail"}, "Check Gmail"),
    ("list_vault_items", {"query": "gmail"}, "Look in 1Password"),
    ("set_plugin_env_reference",
     {"plugin": "gmail", "var_name": "X", "op_ref_or_value": "op://a/b/c"},
     "Save a setting for Gmail"),
    ("remove_plugin_env_reference", {"plugin": "gmail", "var_name": "X"},
     "Remove a setting from Gmail"),
    ("config_git_commit", {"message": "m"}, "Save the changes"),
    ("casa_reload", {"scope": "agent"}, "Apply the changes"),
    ("casa_reload_triggers", {}, "Apply the changes"),
    ("casa_restart_supervised", {}, "Arrange a restart"),
    ("config_trigger_upsert", {"role": "assistant"}, "Write the automation"),
    ("config_trigger_delete", {"role": "assistant"}, "Remove the automation"),
    ("specialist_install_request_approval", {},
     "Check whether your approval is needed"),
    ("specialist_install_commit", {}, "Install the new agent"),
    ("specialist_upgrade", {"slug": "finance"}, "Update Alex"),
    ("specialist_rollback", {"slug": "finance"}, "Put Alex back as it was"),
    ("specialist_uninstall", {"slug": "finance"}, "Remove Alex"),
    ("persona_install_commit", {}, "Add the personality"),
    ("persona_apply", {}, "Change the personality"),
    ("resident_persona_swap", {}, "Change the personality"),
    ("resident_persona_reset", {}, "Change the personality"),
    ("persona_remove", {}, "Clear out unused personalities"),
    ("persona_prune", {}, "Clear out unused personalities"),
])
def test_every_table_row_has_its_label(tool, args, expected):
    assert _label(_CASA + tool, args) == expected


_HIDDEN_BUILTINS = ("Read", "Glob", "Grep", "Write", "Edit")
_HIDDEN_CASA = (
    "plugin_list", "persona_list", "specialist_install_inspect",
    "persona_install_inspect", "get_item_fields", "emit_completion",
    "trigger_ack_revoke", "callback_ack_revoke", "event_ack_revoke",
    "persona_ack_revoke", "consent_reprompt",
)


def test_stage_row_tools_and_housekeeping_are_hidden():
    assert STAGE_ROW_TOOLS == frozenset({"plugin_add", "plugin_update"})
    for short in STAGE_ROW_TOOLS:
        assert _label(_CASA + short, {"name": "gmail"}) is None
    for name in _HIDDEN_BUILTINS:
        assert _label(name, {"file_path": "/config/x"}) is None
    for short in _HIDDEN_CASA:
        assert _label(_CASA + short) is None


def test_a_target_is_shown_by_display_name():
    assert (_label(_CASA + "plugin_assign",
                   {"name": "gmail", "target": "specialist:finance"})
            == "Set up Gmail for Alex")


def test_an_unknown_role_reads_as_the_role_agent():
    assert (_label(_CASA + "plugin_assign",
                   {"name": "gmail", "target": "specialist:ghost"})
            == "Set up Gmail for the ghost agent")
    assert _label(_CASA + "specialist_upgrade", {"slug": "ghost"}) == \
        "Update the ghost agent"


def test_the_name_helpers():
    assert plugin_label("gmail") == "Gmail"
    assert plugin_label("todo-list") == "Todo List"
    assert plugin_label("bank_feed") == "Bank Feed"
    assert plugin_label("") == "the plugin"
    assert target_label("resident:assistant", _agent_name) == "Ellen"
    assert target_label("finance", _agent_name) == "Alex"
    assert target_label("specialist:", _agent_name) == "the agent"


def test_an_unlisted_tool_shows_no_argument():
    assert _label("mcp__gmail__send", {"body": "s3cret"}) == GENERIC_LABEL
    assert _label(_CASA + "query_engager", {"x": "s3cret"}) == GENERIC_LABEL
    assert _label("Bash", {"command": "s3cret"}) == GENERIC_LABEL
    assert _label("", {}) == GENERIC_LABEL
    assert GENERIC_LABEL == "Work on it"


_NOT_NAMES = ("op://Casa/Gmail/credential", "x" * 200, "Gmail Bot",
              "../etc", "-gmail", "GMAIL")


@pytest.mark.parametrize("bad", _NOT_NAMES)
def test_a_value_that_is_not_a_name_is_never_shown(bad):
    cases = [
        ("plugin_assign", {"name": bad, "target": "specialist:finance"},
         "Set up the plugin for Alex"),
        ("plugin_assign", {"name": "gmail", "target": bad},
         "Set up Gmail for the agent"),
        ("plugin_assign", {"name": "gmail", "target": "specialist:" + bad},
         "Set up Gmail for the agent"),
        ("plugin_unassign", {"name": bad, "target": bad},
         "Take the plugin away from the agent"),
        ("plugin_remove", {"name": bad}, "Remove the plugin"),
        ("verify_plugin_state", {"plugin_name": bad}, "Check the plugin"),
        ("set_plugin_env_reference", {"plugin": bad},
         "Save a setting for the plugin"),
        ("remove_plugin_env_reference", {"plugin": bad},
         "Remove a setting from the plugin"),
        ("specialist_upgrade", {"slug": bad}, "Update the agent"),
        ("specialist_rollback", {"slug": bad}, "Put the agent back as it was"),
        ("specialist_uninstall", {"slug": bad}, "Remove the agent"),
    ]
    for tool, args, expected in cases:
        out = _label(_CASA + tool, args)
        assert out == expected, (tool, args)
        assert bad not in out


def test_a_missing_or_odd_argument_still_gives_a_label():
    assert _label(_CASA + "plugin_assign", {}) == "Set up the plugin for the agent"
    assert _label(_CASA + "plugin_remove", {"name": 7}) == "Remove the plugin"
    assert step_label(_CASA + "plugin_remove", None,  # type: ignore[arg-type]
                      agent_name=_agent_name) == "Remove the plugin"


_FORBIDDEN = ("op_ref_or_value", "var_name", "config", "content",
              "old_string", "new_string", "prompt", "task", "context")


def _tool_names_in_table() -> list[str]:
    return [
        "plugin_assign", "plugin_unassign", "plugin_remove",
        "verify_plugin_state", "list_vault_items", "set_plugin_env_reference",
        "remove_plugin_env_reference", "config_git_commit", "casa_reload",
        "casa_reload_triggers", "casa_restart_supervised",
        "config_trigger_upsert", "config_trigger_delete",
        "specialist_install_request_approval", "specialist_install_commit",
        "specialist_upgrade", "specialist_rollback", "specialist_uninstall",
        "persona_install_commit", "persona_apply", "resident_persona_swap",
        "resident_persona_reset", "persona_remove", "persona_prune",
    ]


def test_no_forbidden_argument_reaches_a_label():
    args = {k: "ZZMARK" for k in _FORBIDDEN}
    args["config"] = {"ZZMARK": "ZZMARK"}
    args.update(name="gmail", plugin="gmail", plugin_name="gmail",
                target="specialist:finance", slug="finance")
    for short in _tool_names_in_table():
        out = _label(_CASA + short, dict(args))
        assert out is not None and out != GENERIC_LABEL, short
        assert "ZZMARK" not in out, short
    for name in _HIDDEN_BUILTINS:
        assert _label(name, dict(args)) is None


def test_every_allowed_configurator_tool_is_decided():
    role = (Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt"
            / "casa" / "defaults" / "roles" / "executor" / "configurator"
            / "role.yaml")
    allowed = yaml.safe_load(role.read_text(encoding="utf-8"))["tools"]["allowed"]
    assert len(allowed) > 20
    undecided = [t for t in allowed if _label(t) == GENERIC_LABEL]
    assert undecided == []
